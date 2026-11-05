import io
import warnings
import wave

from PIL import Image, UnidentifiedImageError

MAX_AUDIO = 640044
MAX_IMAGE = 2_000_000
MAX_PIXELS = 4_000_000
MAX_MODEL_EDGE = 1024


def validate_audio(data):
    if len(data) > MAX_AUDIO:
        raise ValueError("audio_too_large")
    try:
        with wave.open(io.BytesIO(data)) as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (
                1,
                2,
                16000,
                "NONE",
            ):
                raise ValueError("audio_requires_mono_16khz_pcm16_wav")
            frames = wav.getnframes()
            if not 1 <= frames <= 320000 or len(wav.readframes(frames)) != frames * 2:
                raise ValueError("invalid_audio_duration_or_data")
    except (wave.Error, EOFError) as exc:
        raise ValueError("invalid_wav") from exc
    return data


def sanitize_image(data):
    if len(data) > MAX_IMAGE:
        raise ValueError("image_too_large")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG"} or image.width * image.height > MAX_PIXELS:
                    raise ValueError("image_requires_jpeg_or_png_under_4mp")
                image.load()
                clean = Image.new("RGB", image.size)
                clean.paste(image.convert("RGB"))
                clean.thumbnail((MAX_MODEL_EDGE, MAX_MODEL_EDGE), Image.Resampling.LANCZOS)
                out = io.BytesIO()
                clean.save(out, format="PNG")
                return out.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("invalid_image") from exc
