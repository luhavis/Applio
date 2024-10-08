import os
import sys
import time
from scipy import signal
from scipy.io import wavfile
import numpy as np
import multiprocessing
import json
from distutils.util import strtobool
import librosa

multiprocessing.set_start_method("spawn", force=True)

now_directory = os.getcwd()
sys.path.append(now_directory)

from rvc.lib.utils import load_audio
from rvc.train.preprocess.slicer import Slicer

# Remove colab logs
import logging

logging.getLogger("pydub").setLevel(logging.WARNING)
logging.getLogger("numba.core.byteflow").setLevel(logging.WARNING)
logging.getLogger("numba.core.ssa").setLevel(logging.WARNING)
logging.getLogger("numba.core.interpreter").setLevel(logging.WARNING)

# Constants
OVERLAP = 0.3
MAX_AMPLITUDE = 0.9
ALPHA = 0.75
HIGH_PASS_CUTOFF = 48
SAMPLE_RATE_16K = 16000


class PreProcess:
    def __init__(self, sr: int, exp_dir: str, per: float):
        self.slicer = Slicer(
            sr=sr,
            threshold=-42,
            min_length=1500,
            min_interval=400,
            hop_size=15,
            max_sil_kept=500,
        )
        self.sr = sr
        self.b_high, self.a_high = signal.butter(
            N=5, Wn=HIGH_PASS_CUTOFF, btype="high", fs=self.sr
        )
        self.per = per
        self.exp_dir = exp_dir
        self.device = "cpu"
        self.gt_wavs_dir = os.path.join(exp_dir, "sliced_audios")
        self.wavs16k_dir = os.path.join(exp_dir, "sliced_audios_16k")
        os.makedirs(self.gt_wavs_dir, exist_ok=True)
        os.makedirs(self.wavs16k_dir, exist_ok=True)

    def _normalize_audio(self, audio: np.ndarray):
        tmp_max = np.abs(audio).max()
        if tmp_max > 2.5:
            return None
        return (audio / tmp_max * (MAX_AMPLITUDE * ALPHA)) + (1 - ALPHA) * audio

    def process_audio_segment(
        self,
        audio_segment: np.ndarray,
        idx0: int,
        idx1: int,
        process_effects: bool,
    ):
        normalized_audio = (
            self._normalize_audio(audio_segment) if process_effects else audio_segment
        )
        if normalized_audio is None:
            return
        wavfile.write(
            os.path.join(self.gt_wavs_dir, f"{idx0}_{idx1}.wav"),
            self.sr,
            normalized_audio.astype(np.float32),
        )
        audio_16k = librosa.resample(
            normalized_audio, orig_sr=self.sr, target_sr=SAMPLE_RATE_16K
        )
        wavfile.write(
            os.path.join(self.wavs16k_dir, f"{idx0}_{idx1}.wav"),
            SAMPLE_RATE_16K,
            audio_16k.astype(np.float32),
        )

    def process_audio(
        self,
        path: str,
        idx0: int,
        cut_preprocess: bool,
        process_effects: bool,
    ):
        audio_length = 0
        try:
            audio = load_audio(path, self.sr)
            audio_length = librosa.get_duration(y=audio, sr=self.sr)
            if process_effects:
                audio = signal.lfilter(self.b_high, self.a_high, audio)
            idx1 = 0
            if cut_preprocess:
                for audio_segment in self.slicer.slice(audio):
                    i = 0
                    while True:
                        start = int(self.sr * (self.per - OVERLAP) * i)
                        i += 1
                        if len(audio_segment[start:]) > (self.per + OVERLAP) * self.sr:
                            tmp_audio = audio_segment[
                                start : start + int(self.per * self.sr)
                            ]
                            self.process_audio_segment(
                                tmp_audio, idx0, idx1, process_effects
                            )
                            idx1 += 1
                        else:
                            tmp_audio = audio_segment[start:]
                            self.process_audio_segment(
                                tmp_audio, idx0, idx1, process_effects
                            )
                            idx1 += 1
                            break
            else:
                self.process_audio_segment(audio, idx0, idx1, process_effects)
        except Exception as e:
            print(f"Error processing audio: {e}")
        return audio_length


def format_duration(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = int(seconds % 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def save_dataset_duration(file_path, dataset_duration):
    try:
        with open(file_path, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}

    formatted_duration = format_duration(dataset_duration)
    new_data = {
        "total_dataset_duration": formatted_duration,
        "total_seconds": dataset_duration,
    }
    data.update(new_data)

    with open(file_path, "w") as f:
        json.dump(data, f, indent=4)


def process_audio_wrapper(args):
    pp, file, cut_preprocess, process_effects = args
    file_path, idx0 = file
    return pp.process_audio(file_path, idx0, cut_preprocess, process_effects)


def preprocess_training_set(
    input_root: str,
    sr: int,
    num_processes: int,
    exp_dir: str,
    per: float,
    cut_preprocess: bool,
    process_effects: bool,
):
    start_time = time.time()
    pp = PreProcess(sr, exp_dir, per)
    print(f"Starting preprocess with {num_processes} processes...")

    files = [
        (os.path.join(input_root, f), idx)
        for idx, f in enumerate(os.listdir(input_root))
        if f.lower().endswith((".wav", ".mp3", ".flac", ".ogg"))
    ]
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=num_processes) as pool:
        audio_length = pool.map(
            process_audio_wrapper,
            [(pp, file, cut_preprocess, process_effects) for file in files],
        )
    audio_length = sum(audio_length)
    save_dataset_duration(
        os.path.join(exp_dir, "model_info.json"), dataset_duration=audio_length
    )
    elapsed_time = time.time() - start_time
    print(
        f"Preprocess completed in {elapsed_time:.2f} seconds. Dataset duration: {format_duration(audio_length)}."
    )


if __name__ == "__main__":
    experiment_directory = str(sys.argv[1])
    input_root = str(sys.argv[2])
    sample_rate = int(sys.argv[3])
    percentage = float(sys.argv[4])
    num_processes = sys.argv[5]
    if num_processes.lower() == "none":
        num_processes = multiprocessing.cpu_count()
    else:
        num_processes = int(num_processes)
    cut_preprocess = strtobool(sys.argv[6])
    process_effects = strtobool(sys.argv[7])

    preprocess_training_set(
        input_root,
        sample_rate,
        num_processes,
        experiment_directory,
        percentage,
        cut_preprocess,
        process_effects,
    )
