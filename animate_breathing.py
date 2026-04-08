"""
Breathing animation generator for existing Heather images.
Supports SVD and Wan 2.1 backends via ComfyUI API.

Usage:
    python animate_breathing.py path/to/heather_image.png
    python animate_breathing.py path/to/image.png --backend wan --prompt "woman breathing softly"
    python animate_breathing.py path/to/image.png --backend svd --motion 40
    python animate_breathing.py path/to/image.png --seeds 42 123 777
"""

import argparse
import json
import os
import sys
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path

COMFYUI_URL = "http://127.0.0.1:8188"
COMFYUI_DIR = Path("C:/Users/groot/ComfyUI")

# ── SVD config ──
SVD_CHECKPOINT = "svd_xt_1_1.safetensors"
SVD_CHECKPOINT_ALT = "svd_xt.safetensors"
SVD_CLIP_VISION = "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors"
SVD_RESOLUTIONS = {
    "landscape": (1024, 576),
    "portrait": (576, 1024),
    "square": (768, 768),
    "photo": (832, 640),
    "photo_portrait": (640, 832),
}

# ── Wan 2.1 config ──
WAN_UNET = "wan2.1_i2v_480p_14B_fp8_e4m3fn.safetensors"
WAN_CLIP = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
WAN_VAE = "wan_2.1_vae.safetensors"
WAN_CLIP_VISION = "clip_vision_h.safetensors"

DEFAULT_SEEDS = [42, 123, 777]


def check_comfyui():
    """Verify ComfyUI is running, return VRAM info."""
    try:
        resp = urllib.request.urlopen(f"{COMFYUI_URL}/system_stats", timeout=5)
        stats = json.loads(resp.read())
        gpu = stats.get("devices", [{}])[0]
        vram_free = gpu.get("vram_free", 0) / (1024**3)
        vram_total = gpu.get("vram_total", 0) / (1024**3)
        print(f"  ComfyUI: running (VRAM: {vram_free:.1f}/{vram_total:.1f} GB free)")
        return True
    except Exception:
        print(f"ERROR: ComfyUI not responding at {COMFYUI_URL}")
        print(f"  Start it: cd {COMFYUI_DIR} && python main.py --listen --port 8188")
        sys.exit(1)


def check_svd_models():
    """Check SVD models exist, return checkpoint name."""
    ckpt_dir = COMFYUI_DIR / "models" / "checkpoints"
    for name in [SVD_CHECKPOINT, SVD_CHECKPOINT_ALT]:
        if (ckpt_dir / name).exists():
            print(f"  SVD model: {name}")
            return name
    print(f"ERROR: No SVD checkpoint in {ckpt_dir}")
    print(f"  Need: {SVD_CHECKPOINT} or {SVD_CHECKPOINT_ALT}")
    sys.exit(1)


def check_wan_models():
    """Check all Wan 2.1 models exist."""
    checks = [
        ("diffusion_models", WAN_UNET, "UNET"),
        ("text_encoders", WAN_CLIP, "T5 text encoder"),
        ("vae", WAN_VAE, "VAE"),
        ("clip_vision", WAN_CLIP_VISION, "CLIP vision"),
    ]
    missing = []
    for folder, filename, label in checks:
        path = COMFYUI_DIR / "models" / folder / filename
        if path.exists():
            size_gb = path.stat().st_size / (1024**3)
            print(f"  Wan {label}: {filename} ({size_gb:.1f} GB)")
        else:
            missing.append((folder, filename, label))

    if missing:
        print(f"\nERROR: Missing Wan 2.1 models:")
        for folder, filename, label in missing:
            print(f"  {label}: {COMFYUI_DIR / 'models' / folder / filename}")
        print(f"\nDownload from: Comfy-Org/Wan_2.1_ComfyUI_repackaged on HuggingFace")
        sys.exit(1)


def pick_resolution(image_path: str, backend: str) -> tuple:
    """Auto-detect best resolution to match source image aspect ratio."""
    try:
        from PIL import Image
        img = Image.open(image_path)
        w, h = img.size
        ratio = w / h
    except Exception:
        default = (832, 480) if backend == "wan" else (832, 640)
        print(f"  WARNING: Couldn't read image, defaulting to {default[0]}x{default[1]}")
        return default

    if backend == "wan":
        # Wan 2.1 480p: must be multiples of 16, max ~832x480
        if ratio > 1.5:
            res = (832, 480)
        elif ratio > 1.1:
            res = (832, 624)
        elif ratio > 0.9:
            res = (672, 672)
        elif ratio > 0.65:
            res = (624, 832)
        else:
            res = (480, 832)
    else:
        # SVD resolution presets
        if ratio > 1.5:
            res = SVD_RESOLUTIONS["landscape"]
        elif ratio > 1.1:
            res = SVD_RESOLUTIONS["photo"]
        elif ratio > 0.9:
            res = SVD_RESOLUTIONS["square"]
        elif ratio > 0.65:
            res = SVD_RESOLUTIONS["photo_portrait"]
        else:
            res = SVD_RESOLUTIONS["portrait"]

    print(f"  Source: {w}x{h} (ratio {ratio:.2f}) -> {backend.upper()}: {res[0]}x{res[1]}")
    return res


def build_svd_workflow(image_filename: str, svd_model: str, seed: int,
                       motion_bucket: int, frames: int, fps: int,
                       augmentation: float, cfg: float, steps: int,
                       prefix: str, resolution: tuple) -> dict:
    """Build ComfyUI workflow for SVD img2vid."""
    svd_w, svd_h = resolution
    return {
        "1": {
            "inputs": {"ckpt_name": svd_model},
            "class_type": "ImageOnlyCheckpointLoader",
        },
        "2": {
            "inputs": {"image": image_filename, "upload": "image"},
            "class_type": "LoadImage",
        },
        "3": {
            "inputs": {
                "clip_vision": ["1", 1], "init_image": ["2", 0], "vae": ["1", 2],
                "width": svd_w, "height": svd_h, "video_frames": frames,
                "motion_bucket_id": motion_bucket, "fps": fps,
                "augmentation_level": augmentation,
            },
            "class_type": "SVD_img2vid_Conditioning",
        },
        "4": {
            "inputs": {
                "seed": seed, "steps": steps, "cfg": cfg,
                "sampler_name": "euler", "scheduler": "karras", "denoise": 1.0,
                "model": ["1", 0], "positive": ["3", 0],
                "negative": ["3", 1], "latent_image": ["3", 2],
            },
            "class_type": "KSampler",
        },
        "5": {
            "inputs": {"samples": ["4", 0], "vae": ["1", 2]},
            "class_type": "VAEDecode",
        },
        "6": {
            "inputs": {"images": ["5", 0], "fps": float(fps)},
            "class_type": "CreateVideo",
        },
        "7": {
            "inputs": {"video": ["6", 0], "filename_prefix": prefix, "format": "mp4", "codec": "h264"},
            "class_type": "SaveVideo",
        },
        "8": {
            "inputs": {
                "images": ["5", 0], "filename_prefix": prefix,
                "fps": float(fps), "lossless": False, "quality": 85, "method": "default",
            },
            "class_type": "SaveAnimatedWEBP",
        },
    }


def build_wan_workflow(image_filename: str, seed: int, frames: int, fps: int,
                       cfg: float, steps: int, prompt: str, negative: str,
                       prefix: str, resolution: tuple) -> dict:
    """Build ComfyUI workflow for Wan 2.1 Image-to-Video."""
    wan_w, wan_h = resolution
    return {
        # Load Wan UNET (fp8 for VRAM efficiency)
        "1": {
            "inputs": {"unet_name": WAN_UNET, "weight_dtype": "fp8_e4m3fn"},
            "class_type": "UNETLoader",
        },
        # Load T5 text encoder (offload to CPU if needed via 'device' param)
        "2": {
            "inputs": {"clip_name": WAN_CLIP, "type": "wan"},
            "class_type": "CLIPLoader",
        },
        # Load Wan VAE
        "3": {
            "inputs": {"vae_name": WAN_VAE},
            "class_type": "VAELoader",
        },
        # Load CLIP vision for image conditioning
        "4": {
            "inputs": {"clip_name": WAN_CLIP_VISION},
            "class_type": "CLIPVisionLoader",
        },
        # Load source image
        "5": {
            "inputs": {"image": image_filename, "upload": "image"},
            "class_type": "LoadImage",
        },
        # Encode CLIP vision from source image
        "6": {
            "inputs": {"clip_vision": ["4", 0], "image": ["5", 0], "crop": "center"},
            "class_type": "CLIPVisionEncode",
        },
        # Positive prompt
        "7": {
            "inputs": {"text": prompt, "clip": ["2", 0]},
            "class_type": "CLIPTextEncode",
        },
        # Negative prompt
        "8": {
            "inputs": {"text": negative, "clip": ["2", 0]},
            "class_type": "CLIPTextEncode",
        },
        # Wan Image-to-Video latent generation
        "9": {
            "inputs": {
                "positive": ["7", 0], "negative": ["8", 0], "vae": ["3", 0],
                "width": wan_w, "height": wan_h,
                "length": frames, "batch_size": 1,
                "clip_vision_output": ["6", 0], "start_image": ["5", 0],
            },
            "class_type": "WanImageToVideo",
        },
        # KSampler — WanImageToVideo outputs: [0]=positive, [1]=negative, [2]=latent
        "10": {
            "inputs": {
                "seed": seed, "steps": steps, "cfg": cfg,
                "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0,
                "model": ["1", 0], "positive": ["9", 0],
                "negative": ["9", 1], "latent_image": ["9", 2],
            },
            "class_type": "KSampler",
        },
        # VAE decode
        "11": {
            "inputs": {"samples": ["10", 0], "vae": ["3", 0]},
            "class_type": "VAEDecode",
        },
        # Create video
        "12": {
            "inputs": {"images": ["11", 0], "fps": float(fps)},
            "class_type": "CreateVideo",
        },
        # Save MP4
        "13": {
            "inputs": {"video": ["12", 0], "filename_prefix": prefix, "format": "mp4", "codec": "h264"},
            "class_type": "SaveVideo",
        },
        # Save WebP preview
        "14": {
            "inputs": {
                "images": ["11", 0], "filename_prefix": prefix,
                "fps": float(fps), "lossless": False, "quality": 85, "method": "default",
            },
            "class_type": "SaveAnimatedWEBP",
        },
    }


def upload_image(image_path: str) -> str:
    """Upload source image to ComfyUI and return the server filename."""
    image_path = Path(image_path)
    if not image_path.exists():
        print(f"ERROR: Source image not found: {image_path}")
        sys.exit(1)

    with open(image_path, "rb") as f:
        image_data = f.read()

    boundary = uuid.uuid4().hex
    filename = image_path.name
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + image_data + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{COMFYUI_URL}/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read())
        server_name = result.get("name", filename)
        print(f"  Uploaded: {filename} -> {server_name}")
        return server_name
    except Exception as e:
        print(f"ERROR: Failed to upload image: {e}")
        sys.exit(1)


def queue_workflow(workflow: dict, client_id: str) -> str:
    """Queue a workflow on ComfyUI and return the prompt_id."""
    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = urllib.request.Request(
        f"{COMFYUI_URL}/prompt", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read())
        return result.get("prompt_id", "unknown")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"ERROR: ComfyUI rejected workflow: {error_body[:500]}")
        sys.exit(1)


def wait_for_completion(prompt_id: str, timeout_sec: int = 600) -> bool:
    """Poll ComfyUI history until the prompt completes or times out."""
    start = time.time()
    last_dot = start
    while time.time() - start < timeout_sec:
        try:
            resp = urllib.request.urlopen(f"{COMFYUI_URL}/history/{prompt_id}", timeout=10)
            history = json.loads(resp.read())
            if prompt_id in history:
                status = history[prompt_id].get("status", {})
                if status.get("completed", False):
                    return True
                if status.get("status_str") == "error":
                    msgs = status.get("messages", [])
                    print(f"\n  ERROR: {msgs}")
                    return False
        except Exception:
            pass
        if time.time() - last_dot >= 5:
            print(".", end="", flush=True)
            last_dot = time.time()
        time.sleep(2)
    print(f"\n  TIMEOUT after {timeout_sec}s")
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Generate breathing animation from a static Heather image"
    )
    parser.add_argument("source_image", help="Path to source PNG/JPG image")
    parser.add_argument("--backend", choices=["svd", "wan"], default="wan",
                        help="Video backend: wan (better faces) or svd (default: wan)")
    parser.add_argument("--motion", type=int, default=40,
                        help="SVD motion bucket (20=subtle, 40=breathing, 80=dynamic). Ignored for Wan.")
    parser.add_argument("--frames", type=int, default=None,
                        help="Video frames (default: 25 for SVD, 81 for Wan)")
    parser.add_argument("--fps", type=int, default=16,
                        help="Output FPS (default: 16)")
    parser.add_argument("--cfg", type=float, default=None,
                        help="CFG scale (default: 2.5 for SVD, 3.0 for Wan)")
    parser.add_argument("--steps", type=int, default=None,
                        help="Sampling steps (default: 20 for SVD, 30 for Wan)")
    parser.add_argument("--prompt", type=str,
                        default="woman breathing gently, subtle chest rise and fall, natural micro-movements, hair swaying slightly, photorealistic, smooth motion",
                        help="Wan text prompt describing desired motion")
    parser.add_argument("--negative", type=str,
                        default="blurry, distorted face, warped features, extra limbs, deformed, low quality, static, frozen, jerky motion",
                        help="Negative prompt (Wan only)")
    parser.add_argument("--augmentation", type=float, default=0.0,
                        help="SVD augmentation level (default: 0.0)")
    parser.add_argument("--seeds", type=int, nargs="+", default=DEFAULT_SEEDS,
                        help="Seeds for batch variations (default: 42 123 777)")
    parser.add_argument("--timeout", type=int, default=2400,
                        help="Max seconds per generation (default: 2400 for Wan, 900 for SVD)")
    args = parser.parse_args()

    # Set backend-specific defaults
    if args.frames is None:
        args.frames = 41 if args.backend == "wan" else 25
    if args.cfg is None:
        args.cfg = 3.0 if args.backend == "wan" else 2.5
    if args.steps is None:
        args.steps = 20 if args.backend == "wan" else 20

    backend_name = "Wan 2.1" if args.backend == "wan" else "SVD"
    print("=" * 60)
    print(f"Heather Breathing Animation ({backend_name})")
    print("=" * 60)

    source = Path(args.source_image)
    if not source.exists():
        print(f"ERROR: Source image not found: {source}")
        sys.exit(1)

    print(f"Source:  {source}")
    print(f"Backend: {backend_name}")
    print(f"Frames:  {args.frames} | FPS: {args.fps} | CFG: {args.cfg} | Steps: {args.steps}")
    print(f"Seeds:   {args.seeds}")
    if args.backend == "wan":
        print(f"Prompt:  {args.prompt[:80]}...")
    else:
        print(f"Motion:  {args.motion} | Augmentation: {args.augmentation}")
    print()

    # Check prerequisites
    print("Checking prerequisites...")
    check_comfyui()
    if args.backend == "wan":
        check_wan_models()
    else:
        svd_model = check_svd_models()
    print()

    # Detect resolution
    print("Detecting resolution...")
    resolution = pick_resolution(str(source), args.backend)
    print()

    # Upload image
    print("Uploading source image...")
    image_filename = upload_image(str(source))
    print()

    # Generate variations
    client_id = uuid.uuid4().hex[:12]
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    stem = source.stem
    tag = "wan" if args.backend == "wan" else "svd"

    for i, seed in enumerate(args.seeds, 1):
        prefix = f"animated/{stem}_{tag}_s{seed}_{timestamp}"
        print(f"[{i}/{len(args.seeds)}] Generating seed {seed}...")

        if args.backend == "wan":
            workflow = build_wan_workflow(
                image_filename=image_filename, seed=seed,
                frames=args.frames, fps=args.fps,
                cfg=args.cfg, steps=args.steps,
                prompt=args.prompt, negative=args.negative,
                prefix=prefix, resolution=resolution,
            )
        else:
            workflow = build_svd_workflow(
                image_filename=image_filename, svd_model=svd_model, seed=seed,
                motion_bucket=args.motion, frames=args.frames, fps=args.fps,
                augmentation=args.augmentation, cfg=args.cfg, steps=args.steps,
                prefix=prefix, resolution=resolution,
            )

        prompt_id = queue_workflow(workflow, client_id)
        print(f"  Queued: {prompt_id}")
        print(f"  Waiting", end="", flush=True)

        ok = wait_for_completion(prompt_id, timeout_sec=args.timeout)
        if ok:
            print(f"\n  Done! -> {prefix}")
        else:
            print(f"\n  Failed for seed {seed}, continuing...")
        print()

    print("=" * 60)
    print(f"Complete! Videos in: {COMFYUI_DIR / 'output' / 'animated'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
