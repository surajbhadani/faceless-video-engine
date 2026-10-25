"""AI-generated scene images (free tiers) turned into Ken Burns motion clips.

Source order: Hugging Face Inference API (FLUX.1-schnell) -> Cloudflare Workers AI
(SDXL/Flux) -> Pollinations (keyless, slower due to its 45s/IP cooldown). The first
two are free, no-card API tiers that avoid Pollinations' rate-limit delay; either
can be left unconfigured and the chain just skips to the next one. Any failure
returns False and the caller falls back to stock footage, so this layer can never
break a build. Generated images live in the run's workdir and die with it —
nothing is kept.
"""
import os
import random
import subprocess
import time
import urllib.parse
from pathlib import Path

import requests

# House look for generated visuals. A channel whose identity depends on where the
# footage appears to be shot overrides this with config video.ai_style — the search
# term itself cannot carry that, because the same term is also a Pexels query and
# Pexels has little footage outside the usual Western defaults.
DEFAULT_STYLE = ("cinematic photograph, moody dramatic lighting, shallow depth of field, "
                 "photorealistic, high detail, single clear literal subject, "
                 "no text, no words, no watermark")

POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"
HF_MODEL = "black-forest-labs/FLUX.1-schnell"
# HF retired the old api-inference.huggingface.co host and dropped FLUX from the
# free "hf-inference" provider (GPU models moved behind billed routed providers).
# Requests now go through the router to Together AI, which still serves this
# model free (FLUX.1-schnell-Free) when routed with just an HF token. The API
# is OpenAI-compatible: JSON in, JSON out with either a result URL or b64 data.
HF_URL = "https://router.huggingface.co/together/v1/images/generations"
CF_MODEL = "@cf/black-forest-labs/flux-1-schnell"
CF_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"


def _valid_image(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 20_000:
        return False
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() != ""


def _pollinations(prompt: str, w: int, h: int, out_path: Path) -> bool:
    url = POLLINATIONS_URL.format(prompt=urllib.parse.quote(prompt))
    for attempt in range(3):
        try:
            resp = requests.get(
                url,
                params={"width": w, "height": h, "nologo": "true",
                        "seed": random.randint(1, 10 ** 9), "model": "flux"},
                timeout=120)
            if resp.ok and resp.headers.get("content-type", "").startswith("image/"):
                out_path.write_bytes(resp.content)
                if _valid_image(out_path):
                    return True
            elif resp.status_code == 402:
                # free tier allows only 1 image per ~45s per IP, and the
                # cooldown timer resets on every hit (even failed ones), so
                # back off generously between scenes instead of hammering it
                if attempt < 2:
                    time.sleep(60)
                continue
        except Exception:
            pass
        time.sleep(5)
    return False


def _huggingface(prompt: str, w: int, h: int, out_path: Path) -> bool:
    token = os.getenv("HF_API_TOKEN", "").strip()
    if not token:
        return False
    for attempt in range(3):
        try:
            resp = requests.post(
                HF_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={"model": HF_MODEL, "prompt": prompt,
                      "width": w, "height": h, "steps": 4, "n": 1},
                timeout=60)
            if resp.status_code in (429, 503):  # rate-limited / cold start
                time.sleep(10)
                continue
            if not resp.ok:
                return False
            item = (resp.json().get("data") or [{}])[0]
            if item.get("b64_json"):
                import base64
                out_path.write_bytes(base64.b64decode(item["b64_json"]))
            elif item.get("url"):
                img_resp = requests.get(item["url"], timeout=60)
                if not img_resp.ok:
                    continue
                out_path.write_bytes(img_resp.content)
            else:
                return False
            if _valid_image(out_path):
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def _cloudflare(prompt: str, w: int, h: int, out_path: Path) -> bool:
    account_id = os.getenv("CF_ACCOUNT_ID", "").strip()
    token = os.getenv("CF_API_TOKEN", "").strip()
    if not account_id or not token:
        return False
    try:
        resp = requests.post(
            CF_URL.format(account_id=account_id, model=CF_MODEL),
            headers={"Authorization": f"Bearer {token}"},
            # this model has a fixed output size and rejects width/height
            json={"prompt": prompt, "steps": 4},
            timeout=60)
        if not resp.ok:
            return False
        ctype = resp.headers.get("content-type", "")
        if ctype.startswith("image/"):
            out_path.write_bytes(resp.content)
            return _valid_image(out_path)
        # Workers AI returns JSON with a base64 image for this model
        import base64
        data = resp.json().get("result", {}).get("image")
        if data:
            out_path.write_bytes(base64.b64decode(data))
            return _valid_image(out_path)
    except Exception:
        pass
    return False


def generate_scene_image(term: str, w: int, h: int, out_path: Path,
                         style: str | None = None) -> bool:
    prompt = f"{term}, {style or DEFAULT_STYLE}"
    return (_huggingface(prompt, w, h, out_path)
            or _cloudflare(prompt, w, h, out_path)
            or _pollinations(prompt, w, h, out_path))


def ken_burns(image: Path, seconds: float, w: int, h: int, fps: int, out_path: Path) -> None:
    """Animate a still image with a slow zoom and drift so it feels like footage."""
    frames = max(1, round(seconds * fps))
    z = f"1+0.12*on/{frames}" if random.random() < 0.5 else f"1.12-0.12*on/{frames}"
    drift = random.choice([
        "x='(iw-iw/zoom)/2':y='(ih-ih/zoom)*0.35'",
        f"x='(iw-iw/zoom)*on/{frames}':y='(ih-ih/zoom)/2'",
        f"x='(iw-iw/zoom)*(1-on/{frames})':y='(ih-ih/zoom)/2'",
        "x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2'",
    ])
    # free tiers return ~1024px images; lanczos + gentle unsharp keeps the
    # upscale crisp on phone screens before the zoom crops into it
    vf = (f"scale={w * 2}:{h * 2}:flags=lanczos,unsharp=5:5:0.6:5:5:0.0,"
          f"zoompan=z='{z}':{drift}:d={frames}:s={w}x{h}:fps={fps}")
    subprocess.run(
        ["ffmpeg", "-y", "-loop", "1", "-i", str(image), "-vf", vf,
         "-frames:v", str(frames), "-c:v", "libx264", "-preset", "fast", "-crf", "22",
         "-pix_fmt", "yuv420p", str(out_path)],
        check=True, capture_output=True)
