"""UltraSinger uses AI to automatically create UltraStar song files"""

import copy
import re
import getopt
import os
import sys
import Levenshtein
import librosa

from tqdm import tqdm
from packaging import version

import soundfile as sf

from modules import os_helper
from modules.Audio.denoise import ffmpeg_reduce_noise
from modules.Audio.separation import separate_audio
from modules.Audio.vocal_chunks import (
    export_chunks_from_transcribed_data,
    export_chunks_from_ultrastar_data,
)
from modules.Audio.silence_processing import remove_silence_from_transcription_data, get_silence_sections
from modules.Speech_Recognition.TranscriptionResult import TranscriptionResult
from modules.Ultrastar.ultrastar_score_calculator import Score
from modules.csv_handler import export_transcribed_data_to_csv
from modules.Audio.convert_audio import convert_audio_to_mono_wav, convert_wav_to_mp3
from modules.Audio.youtube import (
    download_youtube_audio,
    download_youtube_thumbnail,
    download_youtube_video,
    get_youtube_title,
)
from modules.console_colors import (
    ULTRASINGER_HEAD,
    blue_highlighted,
    gold_highlighted,
    light_blue_highlighted,
    red_highlighted,
    green_highlighted,
)
from modules.Midi.midi_creator import (
    convert_frequencies_to_notes,
    create_midi_notes_from_pitched_data,
    convert_midi_notes_to_ultrastar_notes,
    most_frequent,
    create_midi_instrument,
    instruments_to_midi
)
from modules.Pitcher.pitcher import (
    get_frequencies_with_high_confidence,
    get_pitch_with_crepe_file,
)
from modules.Speech_Recognition.hyphenation import (
    hyphenation,
    language_check,
    create_hyphenator,
)
from modules.Speech_Recognition.Whisper import transcribe_with_whisper
from modules.Ultrastar import (
    ultrastar_score_calculator,
    ultrastar_writer,
    ultrastar_parser,
)
from modules.Ultrastar.ultrastar_txt import FILE_ENCODING
from modules.help_print import print_support, print_help
from modules.os_helper import check_file_exists
from modules.plot import plot, plot_spectrogram
from modules.musicbrainz_client import get_music_infos
from modules.sheet import create_sheet
from modules.ProcessData import *
from Settings import Settings
from modules.DeviceDetection.device_detection import check_gpu_support

settings = Settings()

def pitch_each_chunk_with_crepe(directory: str) -> list[str]:
    """Pitch each chunk with crepe and return midi notes"""
    print(f"{ULTRASINGER_HEAD} Pitching each chunk with {blue_highlighted('crepe')}")

    midi_notes = []
    for filename in sorted(
        [f for f in os.listdir(directory) if f.endswith(".wav")],
        key=lambda x: int(x.split("_")[1]),
    ):
        filepath = os.path.join(directory, filename)
        # todo: stepsize = duration? then when shorter than "it" it should take the duration. Otherwise there a more notes
        pitched_data = get_pitch_with_crepe_file(
            filepath,
            settings.crepe_model_capacity,
            settings.crepe_step_size,
            settings.tensorflow_device,
        )
        conf_f = get_frequencies_with_high_confidence(
            pitched_data.frequencies, pitched_data.confidence
        )

        notes = convert_frequencies_to_notes(conf_f)
        note = most_frequent(notes)[0][0]

        midi_notes.append(note)
        # todo: Progress?
        # print(filename + " f: " + str(mean))

    return midi_notes


def add_hyphen_to_data(
    transcribed_data: list[TranscribedData], hyphen_words: list[list[str]]
):
    """Add hyphen to transcribed data return new data list"""
    new_data = []

    for i, data in enumerate(transcribed_data):
        if not hyphen_words[i]:
            new_data.append(data)
        else:
            chunk_duration = data.end - data.start
            chunk_duration = chunk_duration / (len(hyphen_words[i]))

            next_start = data.start
            for j in enumerate(hyphen_words[i]):
                hyphenated_word_index = j[0]
                dup = copy.copy(data)
                dup.start = next_start
                next_start = data.end - chunk_duration * (
                    len(hyphen_words[i]) - 1 - hyphenated_word_index
                )
                dup.end = next_start
                dup.word = hyphen_words[i][hyphenated_word_index]
                dup.is_hyphen = True
                if hyphenated_word_index == len(hyphen_words[i]) - 1:
                    dup.is_word_end = True
                else:
                    dup.is_word_end = False
                new_data.append(dup)

    return new_data


def get_bpm_from_data(data, sampling_rate):
    """Get real bpm from audio data"""
    onset_env = librosa.onset.onset_strength(y=data, sr=sampling_rate)
    wav_tempo = librosa.beat.tempo(onset_envelope=onset_env, sr=sampling_rate)

    print(f"{ULTRASINGER_HEAD} BPM is {blue_highlighted(str(round(wav_tempo[0], 2)))}")
    return wav_tempo[0]


def get_bpm_from_file(wav_file: str) -> float:
    """Get real bpm from audio file"""
    data, sampling_rate = librosa.load(wav_file, sr=None)
    return get_bpm_from_data(data, sampling_rate)


def correct_words(recognized_words, word_list_file):
    """Docstring"""
    with open(word_list_file, "r", encoding="utf-8") as file:
        text = file.read()
    word_list = text.split()

    for i, rec_word in enumerate(recognized_words):
        if rec_word.word in word_list:
            continue

        closest_word = min(
            word_list, key=lambda x: Levenshtein.distance(rec_word.word, x)
        )
        print(recognized_words[i].word + " - " + closest_word)
        recognized_words[i].word = closest_word
    return recognized_words


def remove_unecessary_punctuations(transcribed_data: list[TranscribedData]) -> None:
    """Remove unecessary punctuations from transcribed data"""
    punctuation = ".,"
    for i, data in enumerate(transcribed_data):
        data.word = data.word.translate({ord(i): None for i in punctuation})


def hyphenate_each_word(
    language: str, transcribed_data: list[TranscribedData]
) -> list[list[str]] | None:
    """Hyphenate each word in the transcribed data."""
    lang_region = language_check(language)
    if lang_region is None:
        print(
            f"{ULTRASINGER_HEAD} {red_highlighted('Error in hyphenation for language ')} {blue_highlighted(language)}{red_highlighted(', maybe you want to disable it?')}"
        )
        return None

    hyphenated_word = []
    try:
        hyphenator = create_hyphenator(lang_region)
        for i in tqdm(enumerate(transcribed_data)):
            pos = i[0]
            hyphenated_word.append(
                hyphenation(transcribed_data[pos].word, hyphenator)
            )
    except Exception as e:
        print(f"{ULTRASINGER_HEAD} {red_highlighted('Error in hyphenation for language ')} {blue_highlighted(language)}{red_highlighted(', maybe you want to disable it?')}")
        print(f"\t{red_highlighted(f'->{e}')}")
        return None

    return hyphenated_word


def print_version() -> None:
    """Print version text"""
    print()
    print(
        f"{ULTRASINGER_HEAD} {gold_highlighted('*****************************')}"
    )
    print(
        f"{ULTRASINGER_HEAD} {gold_highlighted('UltraSinger Version:')} {light_blue_highlighted(settings.APP_VERSION)}"
    )
    print(
        f"{ULTRASINGER_HEAD} {gold_highlighted('*****************************')}"
    )


def run() -> tuple[str, Score, Score]:
    """The processing function of this program"""
    settings.input_file_is_ultrastar_txt = settings.input_file_path.endswith(".txt")

    if settings.input_file_is_ultrastar_txt:
        # Parse Ultrastar txt
        (
            basename,
            settings.output_folder_path,
            audio_file_path,
            ultrastar_class,
        ) = parse_ultrastar_txt()
        process_data = from_ultrastar_txt(ultrastar_class)
        process_data.basename = basename
        process_data.process_data_paths.audio_output_file_path = audio_file_path
        #todo: only ignore whisper
        settings.ignore_audio = True

    elif settings.input_file_path.startswith("https:"):
        # Youtube
        print(f"{ULTRASINGER_HEAD} {gold_highlighted('full automatic mode')}")
        process_data = ProcessData()
        (
            process_data.basename,
            settings.output_folder_path,
            process_data.process_data_paths.audio_output_file_path,
            process_data.media_info,
        ) = download_from_youtube()
    else:
        # Audio File
        print(f"{ULTRASINGER_HEAD} {gold_highlighted('full automatic mode')}")
        process_data = ProcessData()
        (
            process_data.basename,
            settings.output_folder_path,
            process_data.process_data_paths.audio_output_file_path,
            process_data.media_info,
        ) = infos_from_audio_input_file()

    process_data.process_data_paths.cache_folder_path = (
        os.path.join(settings.output_folder_path, "cache")
        if settings.cache_override_path is None
        else settings.cache_override_path
    )
    # Set processing audio to cache file
    process_data.process_data_paths.processing_audio_path = os.path.join(
        process_data.process_data_paths.cache_folder_path, process_data.basename + ".wav"
    )
    os_helper.create_folder(process_data.process_data_paths.cache_folder_path)

    # Separate vocal from audio
    audio_separation_folder_path = separate_vocal_from_audio(
        process_data.process_data_paths.cache_folder_path, process_data.process_data_paths.audio_output_file_path,
        process_data.process_data_paths.processing_audio_path
    )
    process_data.process_data_paths.vocals_audio_file_path = os.path.join(audio_separation_folder_path, "vocals.wav")
    process_data.process_data_paths.instrumental_audio_file_path = os.path.join(audio_separation_folder_path, "no_vocals.wav")

    # Move instrumental and vocals
    # Todo: this is ultraStar specific
    if settings.create_karaoke and version.parse(settings.format_version) < version.parse("1.1.0"):
        karaoke_output_path = os.path.join(settings.output_folder_path, process_data.basename + " [Karaoke].mp3")
        convert_wav_to_mp3(process_data.process_data_paths.instrumental_audio_file_path, karaoke_output_path)

    if version.parse(settings.format_version) >= version.parse("1.1.0"):
        instrumental_output_path = os.path.join(settings.output_folder_path, process_data.basename + " [Instrumental].mp3")
        convert_wav_to_mp3(process_data.process_data_paths.instrumental_audio_file_path, instrumental_output_path)
        vocals_output_path = os.path.join(settings.output_folder_path, process_data.basename + " [Vocals].mp3")
        convert_wav_to_mp3(process_data.process_data_paths.vocals_audio_file_path, vocals_output_path)

    if settings.use_separated_vocal:
        input_path = process_data.process_data_paths.vocals_audio_file_path
    else:
        input_path = process_data.process_data_paths.audio_output_file_path

    # Denoise vocal audio
    denoised_output_path = os.path.join(
        process_data.process_data_paths.cache_folder_path, process_data.basename + "_denoised.wav"
    )
    denoise_vocal_audio(input_path, denoised_output_path)

    # Convert to mono audio
    mono_output_path = os.path.join(
        process_data.process_data_paths.cache_folder_path, process_data.basename + "_mono.wav"
    )
    convert_audio_to_mono_wav(denoised_output_path, mono_output_path)

    # Mute silence sections
    mute_output_path = os.path.join(
        process_data.process_data_paths.cache_folder_path, process_data.basename + "_mute.wav"
    )
    mute_no_singing_parts(mono_output_path, mute_output_path)

    # Define the audio file to process
    process_data.process_data_paths.processing_audio_path = mute_output_path

    # Audio transcription
    process_data.media_info.language = settings.language
    if not settings.ignore_audio:
        transcription_result = transcribe_audio(process_data.process_data_paths.cache_folder_path, process_data.process_data_paths.processing_audio_path)
        if process_data.media_info.language is None:
            process_data.media_info.language = transcription_result.detected_language

        process_data.transcribed_data = transcription_result.transcribed_data
        remove_unecessary_punctuations(process_data.transcribed_data)

        if settings.hyphenation:
            hyphen_words = hyphenate_each_word(process_data.media_info.language, process_data.transcribed_data)
            if hyphen_words is not None:
                process_data.transcribed_data = add_hyphen_to_data(process_data.transcribed_data, hyphen_words)

        process_data.transcribed_data = remove_silence_from_transcription_data(
            process_data.process_data_paths.processing_audio_path, process_data.transcribed_data
        )

        # todo: do we need to correct words?
        # lyric = 'input/faber_lyric.txt'
        # --corrected_words = correct_words(vosk_speech, lyric)

    # Create audio chunks
    # Fixme
    if settings.create_audio_chunks:
        create_audio_chunks(
            process_data.process_data_paths.cache_folder_path,
            process_data.transcribed_data,
            process_data.process_data_paths.audio_output_file_path,
            ultrastar_class,
        )

    # Pitch the audio
    process_data.pitched_data = pitch_audio(process_data.process_data_paths)

    # Todo: to function
    start_times = []
    end_times = []
    words = []
    if not settings.ignore_audio:

        for i, midi_segment in enumerate(process_data.transcribed_data):
            start_times.append(midi_segment.start)
            end_times.append(midi_segment.end)
            words.append(midi_segment.word)
        process_data.midi_segments = create_midi_notes_from_pitched_data(start_times, end_times, words, process_data.pitched_data)
    # Todo: this is also in converter?
    # else:
        # for i, note_lines in enumerate(ultrastar_class.UltrastarNoteLines):
        #     start_times.append(note_lines.startTime)
        #     end_times.append(note_lines.endTime)
        #     words.append(note_lines.word)
    # midi_segments = create_midi_notes_from_pitched_data(start_times, end_times, words, process_data.pitched_data)

    # fixme: whats that?
    new_transcribed_data = []
    for i, midi_segment in enumerate(process_data.midi_segments):
        transcribed_midi_data = TranscribedData(word=midi_segment.word, start=midi_segment.start, end=midi_segment.end, is_hyphen= None, confidence= 1)
        new_transcribed_data.append(transcribed_midi_data)
    process_data.transcribed_data = new_transcribed_data
    # ----

    # Create plot
    if settings.create_plot:
        plot_spectrogram(process_data.process_data_paths.vocals_audio_file_path, settings.output_folder_path, "vocals.wav")
        plot_spectrogram(process_data.process_data_paths.processing_audio_path, settings.output_folder_path, "processing audio")
        plot(process_data.pitched_data, settings.output_folder_path, process_data.midi_segments)

    # Write Ultrastar txt
    if not settings.ignore_audio:
        real_bpm, ultrastar_file_output = create_ultrastar_txt_from_automation(
            process_data.basename,
            settings.output_folder_path,
            process_data.transcribed_data,
            process_data.process_data_paths.audio_output_file_path,
            process_data.midi_segments,
            process_data.media_info
        )
    else:
        ultrastar_file_output = create_ultrastar_txt_from_ultrastar_data(
            settings.output_folder_path, process_data.media_info.title, process_data.midi_segments
        )

    # Calc Points
    # Todo: UltraStar specific
    simple_score = None
    accurate_score = None
    if settings.calculate_score:
        ultrastar_class, simple_score, accurate_score = calculate_score_points(
            process_data, ultrastar_file_output
        )

    # Add calculated score to Ultrastar txt #Todo: Missing Karaoke
    ultrastar_writer.add_score_to_ultrastar_txt(ultrastar_file_output, simple_score)

    # Midi
    if settings.create_midi:
        create_midi_file(process_data.media_info.bpm, settings.output_folder_path, ultrastar_class, process_data.basename)

    # Sheet music
    create_sheet(process_data.midi_segments, settings, process_data.basename, process_data.media_info, process_data.media_info.bpm)

    # Cleanup
    if not settings.keep_cache:
        remove_cache_folder(process_data.process_data_paths.cache_folder_path)

    # Print Support
    print_support()
    return ultrastar_file_output, simple_score, accurate_score


def mute_no_singing_parts(mono_output_path, mute_output_path):
    print(
        f"{ULTRASINGER_HEAD} Mute audio parts with no singing"
    )
    silence_sections = get_silence_sections(mono_output_path)
    y, sr = librosa.load(mono_output_path, sr=None)
    # Mute the parts of the audio with no singing
    for i in silence_sections:
        # Define the time range to mute

        start_time = i[0]  # Start time in seconds
        end_time = i[1]  # End time in seconds

        # Convert time to sample indices
        start_sample = int(start_time * sr)
        end_sample = int(end_time * sr)

        y[start_sample:end_sample] = 0
    sf.write(mute_output_path, y, sr)


def get_unused_song_output_dir(path: str) -> str:
    """Get an unused song output dir"""
    # check if dir exists and add (i) if it does
    i = 1
    if os_helper.check_if_folder_exists(path):
        path = f"{path} ({i})"
    else:
        return path

    while os_helper.check_if_folder_exists(path):
        path = path.replace(f"({i - 1})", f"({i})")
        i += 1
        if i > 999:
            print(
                f"{ULTRASINGER_HEAD} {red_highlighted('Error: Could not create output folder! (999) is the maximum number of tries.')}"
            )
            raise ValueError("Could not create output folder! (999) is the maximum number of tries.")
    return path


def transcribe_audio(cache_folder_path: str, processing_audio_path: str) -> TranscriptionResult:
    """Transcribe audio with AI"""
    transcription_result = None
    if settings.transcriber == "whisper":
        transcription_config = f"{settings.transcriber}_{settings.whisper_model}_{settings.pytorch_device}_{settings.whisper_align_model}_{settings.whisper_align_model}_{settings.whisper_batch_size}_{settings.whisper_compute_type}_{settings.language}"
        transcription_path = os.path.join(cache_folder_path, f"{transcription_config}.json")
        cached_transcription_available = check_file_exists(transcription_path)
        if settings.skip_cache_transcription or not cached_transcription_available:
            transcription_result = transcribe_with_whisper(
                processing_audio_path,
                settings.whisper_model,
                settings.pytorch_device,
                settings.whisper_align_model,
                settings.whisper_batch_size,
                settings.whisper_compute_type,
                settings.language,
            )
            with open(transcription_path, "w", encoding=FILE_ENCODING) as file:
                file.write(transcription_result.to_json())
        else:
            print(f"{ULTRASINGER_HEAD} {green_highlighted('cache')} reusing cached transcribed data")
            with open(transcription_path) as file:
                json = file.read()
                transcription_result = TranscriptionResult.from_json(json)
    else:
        raise NotImplementedError
    return transcription_result


def separate_vocal_from_audio(cache_folder_path: str, ultrastar_audio_input_path: str, processing_audio_path: str) -> str:
    """Separate vocal from audio"""
    demucs_output_folder = os.path.splitext(
        os.path.basename(ultrastar_audio_input_path)
    )[0]
    audio_separation_path = os.path.join(
        cache_folder_path, "separated", "htdemucs", demucs_output_folder
    )

    vocals_path = os.path.join(audio_separation_path, "vocals.wav")
    instrumental_path = os.path.join(audio_separation_path, "no_vocals.wav")
    if settings.use_separated_vocal or settings.create_karaoke:
        cache_available = check_file_exists(vocals_path) and check_file_exists(
            instrumental_path
        )
        if settings.skip_cache_vocal_separation or not cache_available:
            separate_audio(
                ultrastar_audio_input_path, cache_folder_path, settings.pytorch_device
            )
        else:
            print(f"{ULTRASINGER_HEAD} {green_highlighted('cache')} reusing cached separated vocals")

    if settings.use_separated_vocal:
        input_path = vocals_path
    else:
        input_path = ultrastar_audio_input_path

    convert_audio_to_mono_wav(input_path, processing_audio_path)

    return audio_separation_path

def calculate_score_points(
    processed_data: ProcessData,
    ultrastar_file_output: str,
):
    """Calculate score points"""
    if not settings.ignore_audio:
        ultrastar_txt = ultrastar_parser.parse_ultrastar_txt(ultrastar_file_output)
        (
            simple_score,
            accurate_score,
        ) = ultrastar_score_calculator.calculate_score(processed_data.pitched_data, ultrastar_txt)
        ultrastar_score_calculator.print_score_calculation(simple_score, accurate_score)
    else:
        print(
            f"{ULTRASINGER_HEAD} {blue_highlighted('Score of original Ultrastar txt')}"
        )
        (
            simple_score,
            accurate_score,
        ) = ultrastar_score_calculator.calculate_score(processed_data.pitched_data, processed_data.parsed_file)
        ultrastar_score_calculator.print_score_calculation(simple_score, accurate_score)
        print(
            f"{ULTRASINGER_HEAD} {blue_highlighted('Score of re-pitched Ultrastar txt')}"
        )
        ultrastar_txt = ultrastar_parser.parse_ultrastar_txt(ultrastar_file_output)
        (
            simple_score,
            accurate_score,
        ) = ultrastar_score_calculator.calculate_score(processed_data.pitched_data, ultrastar_txt)
        ultrastar_score_calculator.print_score_calculation(simple_score, accurate_score)
    return ultrastar_txt, simple_score, accurate_score


def create_ultrastar_txt_from_ultrastar_data(
    song_output: str,
    title: str,
    midi_segments: list[MidiSegment],
) -> str:
    """Create Ultrastar txt from Ultrastar data"""
    output_repitched_ultrastar = os.path.join(
        song_output, title + ".txt"
    )
    ultrastar_note_numbers = convert_midi_notes_to_ultrastar_notes(midi_segments)

    ultrastar_writer.create_repitched_txt_from_ultrastar_data(
        settings.input_file_path,
        ultrastar_note_numbers,
        output_repitched_ultrastar,
    )
    return output_repitched_ultrastar


def create_ultrastar_txt_from_automation(
    basename: str,
    song_output: str,
    transcribed_data: list[TranscribedData],
    ultrastar_audio_input_path: str,
    midi_segments: list[MidiSegment],
    media_info: MediaInfo
):
    """Create Ultrastar txt from automation"""
    ultrastar_header = UltrastarTxtValue()
    ultrastar_header.version = settings.format_version
    ultrastar_header.mp3 = basename + ".mp3"
    ultrastar_header.audio = basename + ".mp3"
    ultrastar_header.vocals = basename + " [Vocals].mp3"
    ultrastar_header.instrumental = basename + " [Instrumental].mp3"
    ultrastar_header.video = basename + ".mp4"
    ultrastar_header.language = media_info.language
    cover = basename + " [CO].jpg"
    ultrastar_header.cover = (
        cover if os_helper.check_file_exists(os.path.join(song_output, cover)) else None
    )
    ultrastar_header.creator = f"{ultrastar_header.creator} {Settings.APP_VERSION}"
    ultrastar_header.comment = f"{ultrastar_header.comment} {Settings.APP_VERSION}"

    # Additional data
    ultrastar_header.title = basename
    if media_info.title is not None:
        ultrastar_header.title = media_info.title
    ultrastar_header.artist = basename
    if media_info.artist is not None:
        ultrastar_header.artist = media_info.artist
    if media_info.year is not None:
        ultrastar_header.year = extract_year(media_info.year)
    if media_info.genre is not None:
        ultrastar_header.genre = format_separated_string(media_info.genre)

    ultrastar_note_numbers = convert_midi_notes_to_ultrastar_notes(midi_segments)

    real_bpm = get_bpm_from_file(ultrastar_audio_input_path) # todo: Wrong place
    ultrastar_file_output = os.path.join(song_output, basename + ".txt")
    ultrastar_writer.create_ultrastar_txt_from_automation(
        transcribed_data,
        ultrastar_note_numbers,
        ultrastar_file_output,
        ultrastar_header,
        real_bpm,
    )
    if settings.create_karaoke and version.parse(settings.format_version) < version.parse("1.1.0"):
        title = basename + " [Karaoke]"
        ultrastar_header.title = title
        ultrastar_header.mp3 = title + ".mp3"
        karaoke_output_path = os.path.join(song_output, title)
        karaoke_txt_output_path = karaoke_output_path + ".txt"
        ultrastar_writer.create_ultrastar_txt_from_automation(
            transcribed_data,
            ultrastar_note_numbers,
            karaoke_txt_output_path,
            ultrastar_header,
            real_bpm,
        )
    return real_bpm, ultrastar_file_output

def extract_year(date: str) -> str:
    match = re.search(r'\b\d{4}\b', date)
    if match:
        return match.group(0)
    else:
        return date

def format_separated_string(data: str) -> str:
    temp = re.sub(r'[;/]', ',', data)
    words = temp.split(',')
    words = [s for s in words if s.strip()]

    for i, word in enumerate(words):
        if "-" not in word:
            words[i] = word.strip().capitalize() + ', '
        else:
            dash_words = word.split('-')
            capitalized_dash_words = [dash_word.strip().capitalize() for dash_word in dash_words]
            formatted_dash_word = '-'.join(capitalized_dash_words) + ', '
            words[i] = formatted_dash_word

    formatted_string = ''.join(words)

    if formatted_string.endswith(', '):
        formatted_string = formatted_string[:-2]

    return formatted_string

def infos_from_audio_input_file() -> tuple[str, str, str, MediaInfo]:
    """Infos from audio input file"""
    basename = os.path.basename(settings.input_file_path)
    basename_without_ext = os.path.splitext(basename)[0]

    artist, title = None, None
    if " - " in basename_without_ext:
        artist, title = basename_without_ext.split(" - ", 1)
        search_string = f"{artist} - {title}"
    else:
        search_string = basename_without_ext

    # Get additional data for song
    (title_info, artist_info, year_info, genre_info) = get_music_infos(search_string)

    if title_info is not None:
        title = title_info
        artist = artist_info

    if artist is not None and title is not None:
        basename_without_ext = f"{artist} - {title}"
        extension = os.path.splitext(basename)[1]
        basename = f"{basename_without_ext}{extension}"

    song_output = os.path.join(settings.output_folder_path, basename_without_ext)
    song_output = get_unused_song_output_dir(song_output)
    os_helper.create_folder(song_output)
    os_helper.copy(settings.input_file_path, song_output)
    os_helper.rename(
        os.path.join(song_output, os.path.basename(settings.input_file_path)),
        os.path.join(song_output, basename),
    )
    # Todo: Read ID3 tags
    ultrastar_audio_input_path = os.path.join(song_output, basename)
    return (
        basename_without_ext,
        song_output,
        ultrastar_audio_input_path,
        MediaInfo(artist=artist, title=title, year=year_info, genre=genre_info),
    )


FILENAME_REPLACEMENTS = (('?:"', ""), ("<", "("), (">", ")"), ("/\\|*", "-"))


def sanitize_filename(fname: str) -> str:
    """Sanitize filename"""
    for old, new in FILENAME_REPLACEMENTS:
        for char in old:
            fname = fname.replace(char, new)
    if fname.endswith("."):
        fname = fname.rstrip(" .")  # Windows does not like trailing periods
    return fname


def download_from_youtube() -> tuple[str, str, str, MediaInfo]:
    """Download from YouTube"""
    (artist, title) = get_youtube_title(settings.input_file_path)

    # Get additional data for song
    (title_info, artist_info, year_info, genre_info) = get_music_infos(
        f"{artist} - {title}"
    )

    if title_info is not None:
        title = title_info
        artist = artist_info

    basename_without_ext = sanitize_filename(f"{artist} - {title}")
    basename = basename_without_ext + ".mp3"
    song_output = os.path.join(settings.output_folder_path, basename_without_ext)
    song_output = get_unused_song_output_dir(song_output)
    os_helper.create_folder(song_output)
    download_youtube_audio(settings.input_file_path, basename_without_ext, song_output)
    download_youtube_video(settings.input_file_path, basename_without_ext, song_output)
    thumbnail_url = download_youtube_thumbnail(
        settings.input_file_path, basename_without_ext, song_output
    )
    audio_file_path = os.path.join(song_output, basename)
    return (
        basename_without_ext,
        song_output,
        audio_file_path,
        MediaInfo(artist=artist, title=title, year=year_info, genre=genre_info, youtube_thumbnail_url=thumbnail_url),
    )


def parse_ultrastar_txt() -> tuple[str, str, str, UltrastarTxtValue]:
    """Parse Ultrastar txt"""
    ultrastar_class = ultrastar_parser.parse_ultrastar_txt(settings.input_file_path)

    if ultrastar_class.mp3:
        ultrastar_mp3_name = ultrastar_class.mp3
    elif ultrastar_class.audio:
        ultrastar_mp3_name = ultrastar_class.audio
    else:
        print(
            f"{ULTRASINGER_HEAD} {red_highlighted('Error!')} The provided text file does not have a reference to "
            f"an audio file."
        )
        exit(1)

    dirname = os.path.dirname(settings.input_file_path)
    song_output = os.path.join(
        settings.output_folder_path,
        ultrastar_class.artist.strip() + " - " + ultrastar_class.title.strip(),
    )
    song_output = get_unused_song_output_dir(str(song_output))
    os_helper.create_folder(song_output)

    basename_without_ext = f"{ultrastar_class.artist.strip()} - {ultrastar_class.title.strip()}"
    audio_file_path = os.path.join(dirname, ultrastar_mp3_name)
    return (
        basename_without_ext,
        song_output,
        str(audio_file_path),
        ultrastar_class,
    )


def create_midi_file(
    real_bpm: float,
    song_output: str,
    midi_segments: list[MidiSegment],
    basename_without_ext: str,
) -> None:
    """Create midi file"""
    print(f"{ULTRASINGER_HEAD} Creating Midi with {blue_highlighted('pretty_midi')}")

    # voice_instrument = [
    #     convert_ultrastar_to_midi_instrument(ultrastar_class)
    # ]
    voice_instrument = [
        create_midi_instrument(midi_segments)
    ]

    midi_output = os.path.join(song_output, f"{basename_without_ext}.mid")
    instruments_to_midi(voice_instrument, real_bpm, midi_output, midi_segments)


def pitch_audio(
    process_data_paths: ProcessDataPaths) -> PitchedData:
    """Pitch audio"""
    # todo: chunk pitching as option?
    # midi_notes = pitch_each_chunk_with_crepe(chunk_folder_name)

    pitching_config = f"crepe_{settings.ignore_audio}_{settings.crepe_model_capacity}_{settings.crepe_step_size}_{settings.tensorflow_device}"
    pitched_data_path = os.path.join(process_data_paths.cache_folder_path, f"{pitching_config}.json")
    cache_available = check_file_exists(pitched_data_path)

    if settings.skip_cache_transcription or not cache_available:
        pitched_data = get_pitch_with_crepe_file(
            process_data_paths.processing_audio_path,
            settings.crepe_model_capacity,
            settings.crepe_step_size,
            settings.tensorflow_device,
        )

        pitched_data_json = pitched_data.to_json()
        with open(pitched_data_path, "w", encoding=FILE_ENCODING) as file:
            file.write(pitched_data_json)
    else:
        print(f"{ULTRASINGER_HEAD} {green_highlighted('cache')} reusing cached pitch data")
        with open(pitched_data_path) as file:
            json = file.read()
            pitched_data = PitchedData.from_json(json)

    return pitched_data


def create_audio_chunks(
    cache_folder_path: str,
    transcribed_data: list[TranscribedData],
    ultrastar_audio_input_path: str,
    ultrastar_class: UltrastarTxtValue
) -> None:
    """Create audio chunks"""
    audio_chunks_path = os.path.join(cache_folder_path, settings.audio_chunk_folder_name)
    os_helper.create_folder(audio_chunks_path)
    if not settings.ignore_audio:  # and csv
        csv_filename = os.path.join(audio_chunks_path, "_chunks.csv")
        export_chunks_from_transcribed_data(
            process_data.process_data_paths.processing_audio_path, transcribed_data, audio_chunks_path
        )
        export_transcribed_data_to_csv(transcribed_data, csv_filename)
    else:
        export_chunks_from_ultrastar_data(
            ultrastar_audio_input_path, ultrastar_class, audio_chunks_path
        )

def denoise_vocal_audio(input_path: str, output_path: str) -> None:
    """Denoise vocal audio"""
    cache_available = check_file_exists(output_path)
    if settings.skip_cache_denoise_vocal_audio or not cache_available:
        ffmpeg_reduce_noise(input_path, output_path)
    else:
        print(f"{ULTRASINGER_HEAD} {green_highlighted('cache')} reusing cached denoised audio")

def main(argv: list[str]) -> None:
    """Main function"""
    print_version()
    init_settings(argv)
    run()
    sys.exit()

def remove_cache_folder(cache_folder_path: str) -> None:
    """Remove cache folder"""
    os_helper.remove_folder(cache_folder_path)

def init_settings(argv: list[str]) -> None:
    """Init settings"""
    long, short = arg_options()
    opts, args = getopt.getopt(argv, short, long)
    if len(opts) == 0:
        print_help()
        sys.exit()
    for opt, arg in opts:
        if opt == "-h":
            print_help()
            sys.exit()
        elif opt in ("-i", "--ifile"):
            settings.input_file_path = arg
        elif opt in ("-o", "--ofile"):
            settings.output_folder_path = arg
        elif opt in ("--whisper"):
            settings.transcriber = "whisper"
            settings.whisper_model = arg
        elif opt in ("--whisper_align_model"):
            settings.whisper_align_model = arg
        elif opt in ("--whisper_batch_size"):
            settings.whisper_batch_size = int(arg)
        elif opt in ("--whisper_compute_type"):
            settings.whisper_compute_type = arg
        elif opt in ("--language"):
            settings.language = arg
        elif opt in ("--crepe"):
            settings.crepe_model_capacity = arg
        elif opt in ("--crepe_step_size"):
            settings.crepe_step_size = int(arg)
        elif opt in ("--plot"):
            settings.create_plot = arg in ["True", "true"]
        elif opt in ("--midi"):
            settings.create_midi = arg in ["True", "true"]
        elif opt in ("--hyphenation"):
            settings.hyphenation = eval(arg.title())
        elif opt in ("--disable_separation"):
            settings.use_separated_vocal = not arg
        elif opt in ("--disable_karaoke"):
            settings.create_karaoke = not arg
        elif opt in ("--create_audio_chunks"):
            settings.create_audio_chunks = arg
        elif opt in ("--ignore_audio"):
            settings.ignore_audio = arg in ["True", "true"]
        elif opt in ("--force_cpu"):
            settings.force_cpu = arg
            if settings.force_cpu:
                os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
        elif opt in ("--force_whisper_cpu"):
            settings.force_whisper_cpu = eval(arg.title())
        elif opt in ("--force_crepe_cpu"):
            settings.force_crepe_cpu = eval(arg.title())
        elif opt in ("--format_version"):
            if arg != '0.3.0' and arg != '1.0.0' and arg != '1.1.0':
                print(
                    f"{ULTRASINGER_HEAD} {red_highlighted('Error: Format version')} {blue_highlighted(arg)} {red_highlighted('is not supported.')}"
                )
                sys.exit(1)
            settings.format_version = arg
        elif opt in ("--keep_cache"):
            settings.keep_cache = arg
        elif opt in ("--musescore_path"):
            settings.musescore_path = arg
    if settings.output_folder_path == "":
        if settings.input_file_path.startswith("https:"):
            dirname = os.getcwd()
        else:
            dirname = os.path.dirname(settings.input_file_path)
        settings.output_folder_path = os.path.join(dirname, "output")

    if not settings.force_cpu:
        settings.tensorflow_device, settings.pytorch_device = check_gpu_support()

    return settings


def arg_options():
    short = "hi:o:amv:"
    long = [
        "ifile=",
        "ofile=",
        "crepe=",
        "crepe_step_size=",
        "whisper=",
        "whisper_align_model=",
        "whisper_batch_size=",
        "whisper_compute_type=",
        "language=",
        "plot=",
        "midi=",
        "hyphenation=",
        "disable_separation=",
        "disable_karaoke=",
        "create_audio_chunks=",
        "ignore_audio=",
        "force_cpu=",
        "force_whisper_cpu=",
        "force_crepe_cpu=",
        "format_version=",
        "keep_cache",
        "musescore_path="
    ]
    return long, short

if __name__ == "__main__":
    main(sys.argv[1:])
