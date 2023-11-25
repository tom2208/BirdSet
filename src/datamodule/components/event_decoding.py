import soundfile as sf
import librosa


class EventDecoding:
    def __init__(self, min_len, max_len=None, sampling_rate=None):
        self.min_len = min_len # in seconds
        self.max_len = max_len
        self.sampling_rate = sampling_rate

    def _load_audio(self, path, start=None, end=None):
        sr = sf.info(path).samplerate
        if start is not None and end is not None:
            if end - start < self.min_len:  # TODO: improve, eg. edge cases, more dynamic loading
                end = start + self.min_len
            if self.max_len and end - start > self.max_len:
                end = start + self.max_len
            start, end = int(start * sr), int(end * sr)
        if not end:
            end = int(self.max_len * sr)

        audio, sr = sf.read(path, start=start, stop=end)

        if audio.ndim != 1:
            audio = audio.swapaxes(1, 0)
            audio = librosa.to_mono(audio)
        if sr != self.sampling_rate:
            audio = librosa.resample(audio, orig_sr=sr, target_sr=self.sampling_rate)
            sr = self.sampling_rate
        return audio, sr

    def __call__(self, batch):
        audios, srs = [], []
        for b_idx in range(len(batch["filepath"])):
            if batch["detected_events"][b_idx]:
                start, end = batch["detected_events"][b_idx]
            else:
                start, end = batch["start_time"][b_idx], batch["end_time"][b_idx]
            audio, sr = self._load_audio(batch["filepath"][b_idx], start, end)
            audios.append(audio)
            srs.append(sr)
        batch["audio"] = [{"path": path, "array": audio, "samplerate": sr} for audio, path, sr in zip(audios, batch["filepath"], srs)]
        return batch