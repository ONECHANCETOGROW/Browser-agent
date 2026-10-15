"""Make a WAV of a spoken sentence for the live voice test, using Windows' built-in speech synthesiser (System.Speech); no network, nothing installed.

Chrome can use a file as its fake microphone (--use-file-for-fake-audio-capture). The sentence is padded with silence before and after so the recogniser
has time to start and to see the end of speech. This is a synthetic voice: it exercises the real recogniser and pipeline, not real-world noise.

usage: python -m tests.e2e.make_test_speech "sentence" out.wav
"""
import subprocess
import sys
import wave
from pathlib import Path


def synthesize(text: str, out: Path, lead_s: float = 2.0, tail_s: float = 3.0) -> dict:
    raw = out.with_suffix(".raw.wav")
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(48000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono); "
          f"$s.SetOutputToWaveFile('{raw}', $f); $s.Rate = 0; $s.Speak('{text.replace(chr(39), chr(39) * 2)}'); $s.Dispose(); Write-Output $s")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60)
    if not raw.is_file() or raw.stat().st_size < 1000:
        raise RuntimeError("speech synthesis failed: " + (r.stderr or r.stdout)[:300])
    with wave.open(str(raw), "rb") as w:
        params, frames = w.getparams(), w.readframes(w.getnframes())
    sil = lambda s: b"\x00\x00" * int(params.framerate * s)
    with wave.open(str(out), "wb") as w:
        w.setparams(params)
        w.writeframes(sil(lead_s) + frames + sil(tail_s))
    raw.unlink()
    speech_s = len(frames) / (2 * params.framerate)
    return {"path": str(out), "rate": params.framerate, "channels": params.nchannels, "speech_s": round(speech_s, 2), "total_s": round(speech_s + lead_s + tail_s, 2), "lead_s": lead_s}


if __name__ == "__main__":
    print(synthesize(sys.argv[1], Path(sys.argv[2])))
