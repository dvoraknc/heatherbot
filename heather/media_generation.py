"""
heather.media_generation — ComfyUI/FLUX Image Generation
=========================================================
FLUX.1 dev pipeline: workflow management, pose detection, prompt building,
ControlNet injection, LoRA management, ReActor faceswap orchestration.

Replaces: heather_telegram_bot.py
  - POSE_MAP: lines 483-547
  - POSE_KEYWORDS: lines 550-582
  - POSE_NSFW_DESCRIPTIONS: lines 585-622
  - HEATHER_PROMPT_PREFIX_SFW/NSFW: lines 469-470
  - HEATHER_PROMPT_SUFFIX/NSFW: lines 471-472
  - HEATHER_NEGATIVE_PROMPT: line 474
  - NSFW_SELFIE_DESCRIPTIONS: lines 4807-4817
  - PROACTIVE_SELFIE_DESCRIPTIONS: lines 3554-3568
  - EMMA_ASK_KEYWORDS: lines 444-453
  - EMMA_PHOTO_CAPTIONS: lines 455-461
  - is_emma_photo_request: lines 463-466
  - _is_nsfw_context: lines 4820-4834
  - extract_image_description: lines 4761-4800
  - extract_photo_context_from_response: lines 4836-4854
  - response_wants_to_send_photo: lines 4802-4805
  - RESPONSE_PHOTO_TRIGGERS: lines 3502-3523
  - load_comfyui_workflow: lines 7941-7946
  - queue_comfyui_prompt: lines 7950-7959
  - get_comfyui_history: lines 7961-7967
  - get_comfyui_image: lines 7969-7981
  - is_valid_image_data: lines 7983-7988
  - detect_pose: lines 7990-7996
  - _get_pose_nsfw_description: lines 7999-8004
  - build_heather_prompt: lines 8007-8022
  - generate_heather_image: lines 8024-8201
  - can_generate_photos: lines 4931-4934
  - check_comfyui_status: lines 6495-6502
  - check_heather_face: lines 6509-6510

Dependencies: heather.config, heather.logging_setup
Used by: heather_telegram_bot.py (image sending, handlers, proactive photos)
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.parse
import urllib.request
from typing import Optional

import requests

from heather import config
from heather.logging_setup import main_logger, PerformanceTimer


# ============================================================================
# PROMPT CONSTANTS — FLUX natural language (no SDXL weighted tokens)
# ============================================================================

HEATHER_PROMPT_PREFIX_SFW = "35mm film photo of a real 48 year old woman with platinum blonde straight shoulder length hair and blue green eyes, thin build with prominent collarbones, thin arms, very pale fair skin with visible aging, small delicate necklace, warm genuine smile, "
HEATHER_PROMPT_PREFIX_NSFW = "35mm film photo of a real 48 year old woman with platinum blonde straight shoulder length hair and blue green eyes, thin build with prominent collarbones, thin arms, saggy mature breasts with pendulous shape hanging low, nipples pointing downward, medium pink areolas, pale fair skin with visible aging and wrinkles, skinny body, small delicate necklace, "
HEATHER_PROMPT_SUFFIX = ", golden hour warm lighting, shot on 35mm film, Kodak Portra 400, shallow depth of field f/1.8 bokeh, natural film grain texture, warm analog tones, authentic candid amateur photo, natural skin imperfections visible, detailed hands with five fingers"
HEATHER_PROMPT_SUFFIX_NSFW = ", golden hour warm lighting, shot on 35mm film, Kodak Portra 400, shallow depth of field f/1.8 bokeh, slight green-teal color shift in shadows, natural film grain, slightly faded colors, warm analog tones, authentic candid unposed, not retouched, natural skin imperfections with pores and wrinkles, two arms only, two legs only, five fingers on each hand"
# FLUX negative prompt — fights the perky/glamour/digital bias
HEATHER_NEGATIVE_PROMPT = "young, smooth skin, perky breasts, round breasts, full breasts, big round boobs, voluptuous, curvy, thick, overweight, glamour, airbrushed, perfect skin, beauty filter, plastic surgery, implants, firm breasts, upturned nipples, upper pole fullness, professional model, studio lighting, perfect body, idealized, magazine, digital look, HDR, oversaturated, clean digital photo, harsh flash, cartoon, anime, 3d render"


# ============================================================================
# POSE CONSTANTS
# ============================================================================

POSE_MAP = {
    "from_behind": {
        "image": "poses/from_behind.png",
        "prompt_boost": "from behind, rear view, back facing camera, looking back over shoulder",
        "landscape": False,
        "skip_face_swap": True,
        "use_controlnet": True,
    },
    "bent_over": {
        "image": "poses/bent_over.png",
        "prompt_boost": "bent over, bending forward, ass up, leaning forward, arms hanging down",
        "landscape": True,
        "skip_face_swap": False,
        "use_controlnet": False,
    },
    "all_fours": {
        "image": "poses/all_fours.png",
        "prompt_boost": "on all fours, hands and knees on bed, back arched, looking at camera",
        "landscape": True,
        "skip_face_swap": False,
        "use_controlnet": False,
    },
    "on_knees": {
        "image": "poses/on_knees.png",
        "prompt_boost": "kneeling upright on bed, knees spread wide apart, arms at sides",
        "landscape": False,
        "skip_face_swap": False,
        "use_controlnet": False,
    },
    "laying_down": {
        "image": "poses/laying_down.png",
        "prompt_boost": "lying flat on her back on a bed, legs spread apart and bent at the knees, hands above head on pillow",
        "landscape": True,
        "skip_face_swap": False,
        "use_controlnet": False,
    },
    "sitting": {
        "image": "poses/sitting.png",
        "prompt_boost": "sitting on the edge of a bed, legs apart, leaning back on hands",
        "landscape": False,
        "skip_face_swap": False,
        "use_controlnet": False,
    },
    "side_view": {
        "image": "poses/side_view.png",
        "prompt_boost": "standing in profile view, side view showing breasts and butt silhouette",
        "landscape": False,
        "skip_face_swap": False,
        "use_controlnet": True,
    },
    "ass_up": {
        "image": "poses/ass_up.png",
        "prompt_boost": "face down ass up, hips elevated, back arched, prone on bed",
        "landscape": True,
        "skip_face_swap": True,
        "use_controlnet": True,
    },
    "spread": {
        "image": "poses/spread.png",
        "prompt_boost": "sitting in a chair with legs spread wide open resting on the armrests, exposed pussy visible",
        "landscape": True,
        "skip_face_swap": False,
        "use_controlnet": False,
    },
}

# Ordered list — more specific phrases first to avoid false matches
POSE_KEYWORDS = [
    ("on all fours", "all_fours"),
    ("hands and knees", "all_fours"),
    ("doggystyle", "all_fours"),
    ("doggy style", "all_fours"),
    ("doggy", "all_fours"),
    ("face down ass up", "ass_up"),
    ("ass up", "ass_up"),
    ("ass in the air", "ass_up"),
    ("bent over", "bent_over"),
    ("bending over", "bent_over"),
    ("bend over", "bent_over"),
    ("from behind", "from_behind"),
    ("from the back", "from_behind"),
    ("back view", "from_behind"),
    ("rear view", "from_behind"),
    ("turn around", "from_behind"),
    ("on your knees", "on_knees"),
    ("kneeling", "on_knees"),
    ("laying down", "laying_down"),
    ("lying down", "laying_down"),
    ("on the bed", "laying_down"),
    ("on your back", "laying_down"),
    ("side view", "side_view"),
    ("side profile", "side_view"),
    ("from the side", "side_view"),
    ("sitting", "sitting"),
    ("seated", "sitting"),
    ("legs spread", "spread"),
    ("spread legs", "spread"),
    ("spread your legs", "spread"),
    ("spread eagle", "spread"),
]

# Pose-specific NSFW descriptions — FLUX natural language
POSE_NSFW_DESCRIPTIONS = {
    "from_behind": [
        "full body photo of a completely nude woman standing facing away from camera, slight S-curve pose, looking back over shoulder with a smile, back and round butt visible, bedroom",
        "full body photo of a completely nude woman standing near a mirror, back facing camera, looking back, playful expression, bedroom",
    ],
    "bent_over": [
        "full body photo of a completely nude woman bent over the edge of a bed, ass up, arms hanging down, looking back over shoulder, bedroom",
        "full body photo of a completely nude woman bending forward, hands on edge of bed, back arched, looking over shoulder with a flirty expression, bedroom",
    ],
    "all_fours": [
        "full body photo of a completely nude woman on all fours on a bed, hands and knees, back arched, looking at camera with a seductive expression, bedroom",
        "full body photo of a completely nude woman crawling on a bed, on hands and knees, head up, playful expression, bedroom",
    ],
    "on_knees": [
        "full body photo of a completely nude woman kneeling upright on a bed, knees spread wide apart, arms relaxed at her sides, looking up at camera with a smile, bedroom",
        "full body photo of a completely nude woman kneeling on a bed, knees apart, hands on thighs, seductive pose, bedroom",
    ],
    "laying_down": [
        "full body wide angle photo of a completely nude woman lying flat on her back on a white bed, legs spread apart and bent at the knees, hands resting above her head on the pillow, exposed pussy visible, bedroom",
        "full body wide angle photo of a completely nude woman lying on her back on a bed, one leg bent, hand in hair, relaxed seductive pose, bedroom",
    ],
    "sitting": [
        "full body photo of a completely nude woman sitting on the edge of a bed, legs apart and feet on the floor, leaning back on her hands, exposed pussy visible, smiling at camera, bedroom",
        "full body photo of a completely nude woman sitting on a couch, one leg tucked under, leaning back, playful smile, living room",
    ],
    "side_view": [
        "full body photo of a completely nude woman standing in profile view, side view showing natural breasts and butt, bedroom lighting",
        "full body photo of a completely nude woman standing by a window in profile, natural light, side silhouette, bedroom",
    ],
    "ass_up": [
        "full body wide angle photo of a completely nude woman face down ass up on a bed, hips elevated, back arched, arms forward, bedroom",
        "full body wide angle photo of a completely nude woman prone on a bed, face down, hips up, back arched, bedroom",
    ],
    "spread": [
        "full body wide angle photo of a completely nude woman sitting in a recliner chair with legs spread wide open resting on the armrests, exposed pussy with protruding labia visible, frontal view, smiling at camera, living room",
        "full body wide angle photo of a completely nude woman lying back on a bed, legs wide apart, arms at sides, exposed pussy visible, bedroom",
    ],
}


# ============================================================================
# SELFIE DESCRIPTIONS
# ============================================================================

NSFW_SELFIE_DESCRIPTIONS = [
    "nude skinny gaunt mature woman standing in bedroom, full body mirror selfie, one hand holding phone, long pendulous saggy breasts resting low against her ribcage, flirty smile, amateur",
    "nude very thin mature woman standing in bathroom, full body photo, long pendulous saggy breasts hanging low, prominent collarbones, thin bony arms, playful expression, amateur candid",
    "nude skinny gaunt mature woman laying on bed, full body wide angle, legs spread, long saggy breasts resting to the sides, exposed pussy with protruding labia visible, hands behind head, amateur",
    "topless skinny mature woman standing in bedroom mirror selfie, wearing only panties, long pendulous saggy breasts hanging low, hand holding phone, thin arms, amateur",
    "nude very thin mature woman sitting on edge of bed, full body, legs apart, long saggy breasts resting on her lap, exposed pussy with protruding labia visible, thin bony frame, amateur candid",
    "nude skinny gaunt mature woman standing by window, full body, natural light, long pendulous saggy breasts hanging low, prominent collarbones, thin arms, hand on hip, amateur",
    "nude very thin mature woman standing in bedroom, full body mirror selfie, arms at sides, long saggy pendulous breasts hanging low against her thin ribcage, confident smile, amateur",
    "nude skinny gaunt mature woman laying on bed, full body wide angle, legs spread, long saggy breasts resting naturally, exposed pussy with protruding labia visible, one hand in hair, amateur",
    "nude very thin mature woman standing in doorway, full body, long pendulous saggy breasts resting low, leaning against frame, thin bony arms, loose aged skin, amateur candid",
    "nude skinny gaunt mature woman sitting on couch, full body, one leg tucked under, long saggy breasts resting on her thin frame, hand on knee, amateur",
]

PROACTIVE_SELFIE_DESCRIPTIONS = [
    "close up selfie, holding phone, bathroom mirror, getting ready, cute outfit, one hand holding phone",
    "selfie angle from above, chin resting on hand, couch, cozy outfit, smiling up at camera",
    "car selfie, sunglasses, one hand on steering wheel, sitting in drivers seat, casual",
    "standing in kitchen, arms crossed, wearing tank top, morning coffee on counter, smiling",
    "mirror selfie, standing, wearing lingerie, bedroom, one hand holding phone, other hand on hip",
    "standing by window, natural light, arms folded, wearing t-shirt, smiling",
    "standing in living room, hand on hip, casual outfit, flirty pose",
    "mirror selfie, standing, cute dress, holding phone, going out",
    "selfie from above, laying on couch, hair spread out, hand near face, cozy",
    "standing in doorway, leaning against frame, arms crossed, casual clothes, flirty smile",
    "close up selfie, hand brushing hair back, natural light, bedroom",
    "sitting at table, chin on hand, coffee cup in other hand, kitchen, morning light",
]


# ============================================================================
# EMMA PHOTO CONSTANTS
# ============================================================================

EMMA_HIKING_PHOTO = "sfw/casual/518393309_24449331331317269_8182893831074081262_n.jpg"
EMMA_HIKING_ID = "sfw_casual_068"

EMMA_ASK_KEYWORDS = [
    "pic of emma", "photo of emma", "picture of emma", "see emma",
    "show me emma", "show emma", "what does emma look like",
    "what emma look", "emma look like", "emma pic", "emma photo",
    "pic with emma", "photo with emma", "picture with emma",
    "pic of your daughter", "photo of your daughter", "picture of your daughter",
    "see your daughter", "show me your daughter", "show your daughter",
    "what does your daughter look like", "daughter look like",
    "you and emma", "you and your daughter",
]

EMMA_PHOTO_CAPTIONS = [
    "That's me and Emma hiking up at Mt Baker last summer, we had the best time \U0001f97e",
    "Omg yes here's us at Mt Baker, she actually kept up with me for once lol \U0001f602",
    "Here's my girl! Mt Baker hike last summer. She's getting so tall it's scary \U0001f97e",
    "This is us! Mt Baker trail, she complained the whole way up but loved it at the top lol",
    "Aww yeah here we are, Mt Baker last summer. My baby's not such a baby anymore \U0001f62d",
]


# ============================================================================
# RESPONSE PHOTO TRIGGERS
# ============================================================================

RESPONSE_PHOTO_TRIGGERS = [
    "let me show you", "wanna see", "want to see", "i'll send you",
    "sending you a pic", "here's a pic", "check this out",
    "take a look", "selfie for you", "pic for you",
    "let me take a selfie", "hold on let me show",
    "i'll show you", "lemme show you", "want a pic",
    "i'd show you", "id show you", "show you everything",
    "show you what", "show you how", "if you were here",
    "wish i could show", "wish i could send",
    "just sent", "sent you a pic", "sent that pic", "sent you a photo",
    "sending a pic", "sending a photo", "sending now",
    "here you go", "hope you like what you see",
    "[pic]", "[photo]", "[selfie]", "[img]",
    "sending you a", "send you a little", "little treat",
    "hold on let me", "let me grab my phone",
    "taking a pic", "taking a photo", "taking a selfie",
    "getting my camera", "getting my phone",
]


# ============================================================================
# MODULE STATE
# ============================================================================

stats = {
    'comfyui_requests': 0,
    'comfyui_failures': 0,
    'images_generated': 0,
}

# Loaded at module init
_comfyui_workflow = None


# ============================================================================
# WORKFLOW LOADING
# ============================================================================

def load_comfyui_workflow(filepath: str) -> dict:
    """Load a ComfyUI workflow JSON file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def init_workflow():
    """Load the ComfyUI workflow at startup."""
    global _comfyui_workflow
    _comfyui_workflow = load_comfyui_workflow(config.COMFYUI_WORKFLOW_FILE)
    if _comfyui_workflow:
        main_logger.info(f"ComfyUI workflow loaded: {config.COMFYUI_WORKFLOW_FILE}")
    else:
        main_logger.warning(f"ComfyUI workflow not found: {config.COMFYUI_WORKFLOW_FILE}")


def get_workflow():
    """Get the loaded workflow (may be None)."""
    return _comfyui_workflow


# ============================================================================
# HEALTH CHECKS
# ============================================================================

def check_comfyui_status() -> tuple:
    """Check if ComfyUI service is available.

    Returns:
        (online: bool, status_message: str)
    """
    try:
        response = requests.get(f"{config.COMFYUI_URL}/system_stats", timeout=5)
        if response.status_code == 200:
            return True, "Online"
        return False, f"HTTP {response.status_code}"
    except Exception:
        return False, "Offline"


def check_heather_face() -> bool:
    """Check if the Heather face image exists for ReActor faceswap."""
    return os.path.exists(config.HEATHER_FACE_IMAGE)


def can_generate_photos() -> bool:
    """Check if photo generation pipeline is available."""
    is_online, _ = check_comfyui_status()
    return is_online and check_heather_face() and _comfyui_workflow is not None


# ============================================================================
# NSFW CONTEXT DETECTION
# ============================================================================

def _is_nsfw_context(text: str) -> bool:
    """Check if text contains NSFW/intimate context."""
    nsfw_words = ["nude", "naked", "topless", "tits", "boobs", "ass", "pussy",
                  "nudes", "strip", "undress", "take it off", "take off",
                  "nothing on", "no clothes", "without clothes", "bare",
                  "show me everything", "show it all", "sexy pic",
                  "naughty", "dirty pic", "spicy", "risque",
                  "titties", "nipple", "nipples", "breasts", "chest",
                  "flash me", "flash your", "show your body",
                  "nsfw", "explicit", "x rated", "x-rated",
                  "show me your body", "full body", "everything off",
                  "fuck", "cock", "dick", "cum", "wet", "horny",
                  "suck", "lick", "moan", "ride me"]
    text_lower = text.lower()
    return any(w in text_lower for w in nsfw_words)


# ============================================================================
# POSE DETECTION
# ============================================================================

def detect_pose(text: str) -> Optional[str]:
    """Scan text for pose keywords, return first matching pose_id or None."""
    text_lower = text.lower()
    for keyword, pose_id in POSE_KEYWORDS:
        if keyword in text_lower:
            return pose_id
    return None


def _get_pose_nsfw_description(pose_id: str) -> str:
    """Get a random pose-specific NSFW description for the given pose."""
    descriptions = POSE_NSFW_DESCRIPTIONS.get(pose_id)
    if descriptions:
        return random.choice(descriptions)
    return random.choice(NSFW_SELFIE_DESCRIPTIONS)


# ============================================================================
# IMAGE DESCRIPTION EXTRACTION
# ============================================================================

def extract_image_description(message: str) -> str:
    """Extract what kind of image the user is asking for from their message.

    Returns a ComfyUI-ready description string, or "" if no description found.
    """
    message_lower = message.lower()
    original = message

    extraction_patterns = [
        ("send us a picture of you ", True),
        ("send us a pic of you ", True),
        ("send me a picture of you ", True),
        ("send me a pic of you ", True),
        ("send a picture of you ", True),
        ("send a pic of you ", True),
        ("picture of you ", True),
        ("pic of you ", True),
        ("photo of you ", True),
    ]

    for pattern, has_of_you in extraction_patterns:
        if pattern in message_lower:
            pos = message_lower.find(pattern)
            description = original[pos + len(pattern):].strip()
            if description and len(description) > 2:
                if _is_nsfw_context(message_lower) or _is_nsfw_context(description):
                    pose_id = detect_pose(message_lower)
                    if pose_id:
                        return _get_pose_nsfw_description(pose_id)
                    return random.choice(NSFW_SELFIE_DESCRIPTIONS)
                return description

    # Handle direct NSFW requests like "send nudes", "show me your tits", etc.
    if _is_nsfw_context(message_lower):
        pose_id = detect_pose(message_lower)
        if pose_id:
            return _get_pose_nsfw_description(pose_id)
        return random.choice(NSFW_SELFIE_DESCRIPTIONS)

    return ""


def extract_photo_context_from_response(response: str) -> str:
    """Try to extract what kind of photo from the AI response context.
    Favors upright/standing compositions that produce good face swaps.
    Detects NSFW context and generates appropriate descriptions."""
    resp_lower = response.lower()
    if _is_nsfw_context(resp_lower):
        return random.choice(NSFW_SELFIE_DESCRIPTIONS)
    if any(w in resp_lower for w in ["bed", "laying", "lying"]):
        return "standing in bedroom, messy hair, flirty smile, wearing tank top, hand on hip"
    if any(w in resp_lower for w in ["shower", "bath", "towel"]):
        return "standing in bathroom mirror, towel, wet hair, flirty, one hand holding phone"
    if any(w in resp_lower for w in ["dress", "outfit", "wearing"]):
        return "mirror selfie, showing off outfit, flirty pose, hand holding phone"
    if any(w in resp_lower for w in ["work", "uber", "driving", "car"]):
        return "car selfie, sunglasses, one hand on steering wheel, sitting in drivers seat, casual"
    if any(w in resp_lower for w in ["cook", "kitchen", "dinner"]):
        return "standing in kitchen, arms crossed, wearing apron, casual, smiling"
    return random.choice(PROACTIVE_SELFIE_DESCRIPTIONS)


# ============================================================================
# EMMA PHOTO DETECTION
# ============================================================================

def is_emma_photo_request(message: str) -> bool:
    """Check if someone is asking to see Emma or a photo with Emma."""
    msg_lower = message.lower()
    return any(kw in msg_lower for kw in EMMA_ASK_KEYWORDS)


# ============================================================================
# RESPONSE PHOTO DETECTION
# ============================================================================

def response_wants_to_send_photo(response: str) -> bool:
    """Check if Heather's AI response mentions sending a photo/selfie."""
    resp_lower = response.lower()
    return any(trigger in resp_lower for trigger in RESPONSE_PHOTO_TRIGGERS)


# ============================================================================
# PROMPT BUILDING
# ============================================================================

def build_heather_prompt(user_description: str) -> str:
    """Build a full FLUX prompt from a user's image description.

    Adds Heather's character prefix (SFW or NSFW) and film-look suffix.
    Description goes FIRST for max CLIP weight on framing/pose cues.
    """
    user_description = user_description.strip().lower()
    remove_prefixes = ["you ", "heather ", "her ", "she "]
    for prefix in remove_prefixes:
        if user_description.startswith(prefix):
            user_description = user_description[len(prefix):]
    is_nsfw = _is_nsfw_context(user_description)
    if is_nsfw:
        prefix = HEATHER_PROMPT_PREFIX_NSFW.rstrip(', ')
        suffix = HEATHER_PROMPT_SUFFIX_NSFW
    else:
        prefix = HEATHER_PROMPT_PREFIX_SFW.rstrip(', ')
        suffix = HEATHER_PROMPT_SUFFIX
    return f"{user_description}, {prefix}{suffix}"


# ============================================================================
# COMFYUI API
# ============================================================================

def queue_comfyui_prompt(workflow: dict) -> str:
    """Queue a prompt to ComfyUI. Returns prompt_id."""
    data = json.dumps({"prompt": workflow}).encode('utf-8')
    req = urllib.request.Request(
        f"{config.COMFYUI_URL}/prompt",
        data=data,
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read().decode('utf-8'))
        return result.get('prompt_id')


def get_comfyui_history(prompt_id: str) -> dict:
    """Get ComfyUI generation history for a prompt."""
    try:
        req = urllib.request.Request(f"{config.COMFYUI_URL}/history/{prompt_id}")
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception:
        return {}


def get_comfyui_image(filename: str, subfolder: str = "", folder_type: str = "output") -> bytes:
    """Fetch a generated image from ComfyUI."""
    try:
        params = urllib.parse.urlencode({
            "filename": filename,
            "subfolder": subfolder,
            "type": folder_type
        })
        req = urllib.request.Request(f"{config.COMFYUI_URL}/view?{params}")
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read()
    except Exception as e:
        main_logger.error(f"Failed to fetch image {filename}: {e}")
        return None


def is_valid_image_data(data: bytes, min_size: int = 5000) -> bool:
    """Validate image data has valid magic bytes and minimum size."""
    if not data or len(data) < min_size:
        return False
    # PNG magic: 89 50 4E 47, JPEG magic: FF D8
    return data[:4] == b'\x89PNG' or data[:2] == b'\xff\xd8'


# ============================================================================
# IMAGE GENERATION
# ============================================================================

def generate_heather_image(user_description: str, progress_callback=None) -> bytes:
    """Generate image with ComfyUI using FLUX.1 dev pipeline.

    Args:
        user_description: What to generate (e.g. "laying on bed, nude")
        progress_callback: Optional callable for progress updates

    Returns:
        Image bytes (PNG/JPEG)

    Raises:
        Exception: On ComfyUI error, timeout, or unavailability
    """
    stats['comfyui_requests'] += 1

    is_online, status_msg = check_comfyui_status()
    if not is_online:
        stats['comfyui_failures'] += 1
        raise Exception("ComfyUI unavailable")

    if not _comfyui_workflow:
        stats['comfyui_failures'] += 1
        raise Exception("Workflow not loaded")

    workflow = json.loads(json.dumps(_comfyui_workflow))
    full_prompt = build_heather_prompt(user_description)
    is_nsfw = _is_nsfw_context(user_description)

    # Randomize seeds (FLUX workflow: node 7 = main KSampler, node 14 = face blend KSampler)
    for node_id in ["7", "14"]:
        if node_id in workflow and "seed" in workflow[node_id].get("inputs", {}):
            workflow[node_id]["inputs"]["seed"] = random.randint(0, 2**53 - 1)

    # Set positive prompt
    if config.COMFYUI_POSITIVE_PROMPT_NODE in workflow:
        workflow[config.COMFYUI_POSITIVE_PROMPT_NODE]["inputs"]["text"] = full_prompt

    # FLUX negative prompt — use anti-glamour tokens for NSFW to fight perky bias
    if config.COMFYUI_NEGATIVE_PROMPT_NODE in workflow:
        workflow[config.COMFYUI_NEGATIVE_PROMPT_NODE]["inputs"]["text"] = HEATHER_NEGATIVE_PROMPT if is_nsfw else ""

    # Set face image for ReActor
    if config.COMFYUI_FACE_IMAGE_NODE in workflow:
        workflow[config.COMFYUI_FACE_IMAGE_NODE]["inputs"]["image"] = os.path.basename(config.HEATHER_FACE_IMAGE)

    # Set FLUX guidance (replaces CFG for FLUX models)
    if "5" in workflow:
        workflow["5"]["inputs"]["guidance"] = config.FLUX_GUIDANCE

    # NSFW: inject NSFW Master LoRA (always) + anatomy LoRA (only when vulva visible)
    if is_nsfw:
        workflow["20"] = {
            "inputs": {
                "lora_name": "NSFW_master.safetensors",
                "strength_model": 0.75,
                "strength_clip": 0.75,
                "model": ["1", 0],
                "clip": ["1", 1],
            },
            "class_type": "LoraLoader",
            "_meta": {"title": "NSFW Master"}
        }
        vulva_keywords = ["pussy", "vulva", "labia", "spread", "laying", "laying_down",
                          "legs apart", "legs spread", "exposed", "closeup", "close up"]
        desc_lower = user_description.lower()
        needs_anatomy_lora = any(kw in desc_lower for kw in vulva_keywords)
        if needs_anatomy_lora:
            workflow["21"] = {
                "inputs": {
                    "lora_name": "flux-female-anatomy.safetensors",
                    "strength_model": 0.5,
                    "strength_clip": 0.5,
                    "model": ["20", 0],
                    "clip": ["20", 1],
                },
                "class_type": "LoraLoader",
                "_meta": {"title": "Anatomy Detail"}
            }
            workflow["7"]["inputs"]["model"] = ["21", 0]
            workflow["3"]["inputs"]["clip"] = ["21", 1]
            workflow["4"]["inputs"]["clip"] = ["21", 1]
            main_logger.info("NSFW image — NSFW Master + anatomy LoRAs injected")
        else:
            workflow["7"]["inputs"]["model"] = ["20", 0]
            workflow["3"]["inputs"]["clip"] = ["20", 1]
            workflow["4"]["inputs"]["clip"] = ["20", 1]
            main_logger.info("NSFW image — NSFW Master LoRA only")

    # ControlNet pose injection — detect pose, inject nodes at runtime
    pose_id = detect_pose(user_description)
    if pose_id and pose_id in POSE_MAP:
        pose_config = POSE_MAP[pose_id]

        # Prepend pose boost to positive prompt
        boosted_prompt = f"{pose_config['prompt_boost']}, {full_prompt}"
        if config.COMFYUI_POSITIVE_PROMPT_NODE in workflow:
            workflow[config.COMFYUI_POSITIVE_PROMPT_NODE]["inputs"]["text"] = boosted_prompt

        # Swap to landscape dimensions for wide poses
        if pose_config.get("landscape"):
            if "6" in workflow:
                workflow["6"]["inputs"]["width"] = 1344
                workflow["6"]["inputs"]["height"] = 768

        # Only inject ControlNet for poses that benefit from it
        if pose_config.get("use_controlnet"):
            workflow["50"] = {
                "inputs": {"image": pose_config["image"], "upload": "image"},
                "class_type": "LoadImage",
                "_meta": {"title": f"Pose Skeleton ({pose_id})"}
            }
            workflow["51"] = {
                "inputs": {"control_net_name": config.CONTROLNET_MODEL},
                "class_type": "ControlNetLoader",
                "_meta": {"title": "FLUX ControlNet Union Pro 2.0"}
            }
            workflow["52"] = {
                "inputs": {
                    "strength": config.CONTROLNET_STRENGTH,
                    "start_percent": 0.0,
                    "end_percent": config.CONTROLNET_END,
                    "positive": ["5", 0],
                    "negative": ["4", 0],
                    "control_net": ["51", 0],
                    "vae": ["1", 2],
                    "image": ["50", 0],
                },
                "class_type": "ControlNetApplySD3",
                "_meta": {"title": "ControlNet Apply (Pose)"}
            }
            workflow["7"]["inputs"]["positive"] = ["52", 0]
            workflow["7"]["inputs"]["negative"] = ["52", 1]
            main_logger.info(f"ControlNet pose injected: {pose_id} (strength={config.CONTROLNET_STRENGTH})")
        else:
            main_logger.info(f"Pose {pose_id} using prompt-only (no ControlNet)")

        # Skip face swap for back-facing poses (ReActor pastes face on back of head)
        if pose_config.get("skip_face_swap"):
            workflow["9"]["inputs"]["images"] = ["8", 0]
            for nid in ["10", "11", "13", "14", "15"]:
                if nid in workflow:
                    del workflow[nid]
            main_logger.info(f"Face swap skipped for {pose_id}")

    with PerformanceTimer('COMFYUI', 'generate', f"desc={user_description[:30]}"):
        prompt_id = queue_comfyui_prompt(workflow)

        if progress_callback:
            progress_callback("\u23f3 Generating...")

        start_time = time.time()
        while time.time() - start_time < config.COMFYUI_TIMEOUT:
            history = get_comfyui_history(prompt_id)
            if prompt_id in history:
                status = history[prompt_id].get('status', {})
                if status.get('status_str') == 'error':
                    msgs = status.get('messages', [])
                    err_msg = "Unknown error"
                    for msg in msgs:
                        if isinstance(msg, list) and len(msg) > 1:
                            err_msg = msg[1].get('exception_message', str(msg))
                    stats['comfyui_failures'] += 1
                    raise Exception(f"ComfyUI error: {err_msg}")

                outputs = history[prompt_id].get('outputs', {})
                for node_id in [config.COMFYUI_FINAL_OUTPUT_NODE, "12"]:
                    node_output = outputs.get(node_id, {})
                    if 'images' in node_output:
                        for img in node_output['images']:
                            image_data = get_comfyui_image(
                                img['filename'],
                                img.get('subfolder', ''),
                                img.get('type', 'output')
                            )
                            if image_data and is_valid_image_data(image_data):
                                stats['images_generated'] += 1
                                main_logger.info(f"Generated FLUX image: {len(image_data)} bytes from node {node_id}")
                                return image_data
                            elif image_data:
                                main_logger.warning(f"Invalid image from node {node_id}: {len(image_data)} bytes")
            time.sleep(2)

    stats['comfyui_failures'] += 1
    raise Exception("Generation timeout")
