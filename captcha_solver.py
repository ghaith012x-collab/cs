#!/usr/bin/env python3
"""
hCaptcha Universal Solver — Free, Self-Contained Edition
=========================================================
Zero paid APIs. Uses your trained brains (models/):
  motion_params.json  — human mouse-behavior stats

Tactics:
  · curl_cffi for TLS fingerprinting (mimics Chrome)
  · Playwright for HSW proof-of-work token generation
  · Synthetic motion data (no multibot.in)
  · Offline pixel-similarity for FunCAPTCHA / Arkose tiles
  · Direct API calls + in-browser drag solving

Requirements:
  pip install curl_cffi playwright opencv-python numpy pillow torch torchvision
  python -m playwright install chromium

Usage:
  # Standalone solve:
  python captcha_solver.py --sitekey a9b5fb07-92ff-493f-86fe-352a2803b3df --host discord.com

  # In a browser script (import helpers):
  from captcha_solver import (
      solve_funcaptcha_pixels,
      extract_hcaptcha_sitekey, read_hcaptcha_token, set_hcaptcha_token_on_page,
  )
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import os
import random
import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

# Lazy / optional imports — not all environments have these.
# They are imported properly inside the functions that need them.
try:
    import cv2
except ImportError:
    cv2 = None  # type: ignore
try:
    import numpy as np
except ImportError:
    np = None  # type: ignore
try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None  # type: ignore
try:
    from PIL import Image, ImageChops
except ImportError:
    Image = ImageChops = None  # type: ignore

# ═══════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
CHROME_VERSION = "130"
HCAPTCHA_API = "https://api2.hcaptcha.com"
DEFAULT_VERSION = "c3663008fb8d8104807d55045f8251cbe96a2f84"

MODELS_DIR = Path(__file__).resolve().parent / "models"

SCREEN_SIZES = [
    (1920, 1080), (1366, 768), (1536, 864),
    (1440, 900), (1280, 720), (1600, 900),
    # -- Structures crossing stuff --
    (r"(?:what|which).*structure.*cross(?:es)?.*water", "bridge"),
    (r"(?:what|which).*structure.*go(?:es)?.*over.*water", "bridge"),
    (r"(?:what|which).*structure.*span(?:s)?.*water", "bridge"),
    (r"(?:what|which).*structure.*cross(?:es)?.*river", "bridge"),
    (r"(?:what|which).*cross(?:es)?.*river", "bridge"),
    (r"(?:what|which).*cross(?:es)?.*water", "bridge"),
    (r"(?:what|which).*go(?:es)?.*over.*river", "bridge"),
    (r"(?:what|which).*go(?:es)?.*under.*water", "tunnel"),
    (r"(?:what|which).*go(?:es)?.*through.*mountain", "tunnel"),
    (r"(?:what|which).*structure.*through.*mountain", "tunnel"),
    (r"(?:what|which).*built.*cross.*water", "bridge"),
    (r"(?:what|which).*connects.*two.*side.*river", "bridge"),
    (r"(?:what|which).*connects.*two.*side.*water", "bridge"),
    (r"(?:what|which).*(?:bridge|tunnel|dam|lighthouse|pier)", ""),
    # -- Buildings / Places --
    (r"(?:what|which).*building.*borrow.*book", "library"),
    (r"(?:what|which).*building.*keep.*book", "library"),
    (r"(?:what|which).*building.*read.*book", "library"),
    (r"(?:what|which).*building.*buy.*food", "supermarket"),
    (r"(?:what|which).*building.*shop.*grocer", "supermarket"),
    (r"(?:what|which).*building.*sick.*people", "hospital"),
    (r"(?:what|which).*building.*treat.*patient", "hospital"),
    (r"(?:what|which).*building.*(?:doctor|nurse).*work", "hospital"),
    (r"(?:what|which).*building.*learn.*student", "school"),
    (r"(?:what|which).*building.*teach.*child", "school"),
    (r"(?:what|which).*building.*pray.*god", "church"),
    (r"(?:what|which).*building.*worship", "church"),
    (r"(?:what|which).*building.*swim", "pool"),
    (r"(?:what|which).*building.*watch.*movie", "cinema"),
    (r"(?:what|which).*building.*fly.*plane", "airport"),
    (r"(?:what|which).*building.*wait.*train", "station"),
    (r"(?:what|which).*building.*stay.*night.*travel", "hotel"),
    (r"(?:what|which).*building.*eat.*meal.*out", "restaurant"),
    (r"(?:what|which).*building.*keep.*money", "bank"),
    (r"(?:what|which).*building.*send.*mail", "post office"),
    (r"(?:what|which).*building.*fill.*gas", "gas station"),
    (r"(?:what|which).*building.*see.*art", "museum"),
    (r"(?:what|which).*building.*see.*animal.*captiv", "zoo"),
    (r"(?:what|which).*building.*trial.*judge", "courthouse"),
    (r"(?:what|which).*building.*criminal.*kept", "prison"),
    (r"(?:what|which).*building.*fire.*truck", "fire station"),
    (r"(?:what|which).*building.*police.*work", "police station"),
    (r"(?:what|which).*building.*sport.*play", "stadium"),
    (r"(?:what|which).*building.*grow.*crop", "farm"),
    (r"(?:what|which).*building.*make.*product", "factory"),
    (r"(?:what|which).*place.*borrow.*book", "library"),
    (r"(?:what|which).*place.*buy.*food", "supermarket"),
    (r"(?:what|which).*place.*sick.*treated", "hospital"),
    (r"(?:what|which).*place.*student.*learn", "school"),
    (r"(?:what|which).*place.*pray", "church"),
    (r"(?:what|which).*place.*swim", "pool"),
    (r"(?:what|which).*place.*watch.*movie", "cinema"),
    (r"(?:what|which).*place.*fly.*plane", "airport"),
    (r"(?:what|which).*place.*wait.*train", "station"),
    (r"(?:what|which).*place.*sleep.*travel", "hotel"),
    (r"(?:what|which).*place.*eat.*meal", "restaurant"),
    (r"(?:what|which).*place.*keep.*money", "bank"),
    (r"(?:what|which).*place.*send.*letter", "post office"),
    (r"(?:what|which).*place.*see.*painting", "museum"),
    # -- Materials / What is X made of --
    (r"(?:what|which).*(?:is|are).*made.*from.*tree", "wood"),
    (r"(?:what|which).*(?:is|are).*made.*from.*sand|glass.*made.*from", "sand"),
    (r"(?:what|which).*(?:is|are).*made.*of.*sand", "glass"),
    (r"(?:what|which).*(?:is|are).*paper.*made.*of", "wood"),
    (r"(?:what|which).*(?:is|are).*glass.*made.*of", "sand"),
    (r"(?:what|which).*(?:is|are).*wine.*made.*of", "grapes"),
    (r"(?:what|which).*(?:is|are).*bread.*made.*of", "flour"),
    (r"(?:what|which).*(?:is|are).*cheese.*made.*of", "milk"),
    (r"(?:what|which).*(?:is|are).*chocolate.*made.*of", "cocoa"),
    (r"(?:what|which).*(?:is|are).*silk.*made.*(?:of|from)", "silkworms"),
    (r"(?:what|which).*(?:is|are).*wool.*made.*(?:of|from)", "sheep"),
    (r"(?:what|which).*(?:is|are).*leather.*made.*(?:of|from)", "cowhide"),
    (r"(?:what|which).*(?:is|are).*butter.*made.*(?:of|from)", "cream"),
    (r"(?:what|which).*(?:is|are).*yogurt.*made.*(?:of|from)", "milk"),
    (r"(?:what|which).*(?:is|are).*diamond.*made.*(?:of|from)", "carbon"),
    (r"(?:what|which).*(?:is|are).*candle.*made.*(?:of|from)", "wax"),
    (r"(?:what|which).*(?:is|are).*soap.*made.*(?:of|from)", "fat"),
    (r"(?:what|which).*(?:is|are).*rubber.*made.*(?:of|from)", "rubber tree"),
    (r"(?:what|which).*(?:is|are).*steel.*made.*(?:of|from)", "iron"),
    (r"(?:what|which).*(?:is|are).*concrete.*made.*(?:of|from)", "cement"),
    (r"(?:what|which).*(?:is|are).*brick.*made.*(?:of|from)", "clay"),
    (r"(?:what|which).*(?:is|are).*honey.*made.*(?:of|from)", "nectar"),
    (r"(?:what|which).*(?:is|are).*pearl.*made.*(?:of|from)", "oyster"),
    (r"(?:what|which).*(?:is|are).*flour.*made.*(?:of|from)", "wheat"),
    (r"(?:what|which).*(?:is|are).*sugar.*made.*(?:of|from)", "sugarcane"),
    (r"(?:what|which).*(?:is|are).*salt.*from", "sea"),
    # -- Actions / What do you do with X --
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*key", "unlock"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*pen", "write"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*pencil", "write"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*knife", "cut"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*scissors", "cut"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*broom", "sweep"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*mop", "mop"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*phone", "call"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*oven", "bake"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*camera", "photograph"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*paintbrush", "paint"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*hammer", "hammer"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*umbrella.*rain", "protect"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*lamp", "light"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*bed", "sleep"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*chair", "sit"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*spoon", "eat"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*book", "read"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*tv", "watch"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*radio", "listen"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*toaster", "toast"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*(?:iron|clothes iron)", "iron"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*shower", "wash"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*toothbrush", "brush"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*microwave", "heat"),
    (r"(?:what|which).*(?:do|can).*(?:you|we).*do.*with.*soap", "wash"),
    # -- Animal homes / habitats --
    (r"(?:what|which).*where.*(?:do|does).*live", ""),
    (r"(?:what|which).*home.*bird", "nest"),
    (r"(?:what|which).*home.*bee", "hive"),
    (r"(?:what|which).*home.*spider", "web"),
    (r"(?:what|which).*home.*ant", "anthill"),
    (r"(?:what|which).*home.*rabbit", "burrow"),
    (r"(?:what|which).*home.*fox", "den"),
    (r"(?:what|which).*home.*bear", "cave"),
    (r"(?:what|which).*home.*fish", "water"),
    (r"(?:what|which).*home.*dog.*domestic", "kennel"),
    (r"(?:what|which).*home.*horse", "stable"),
    (r"(?:what|which).*home.*pig", "sty"),
    (r"(?:what|which).*home.*cow.*shed", "barn"),
    (r"(?:what|which).*home.*chicken", "coop"),
    (r"(?:what|which).*home.*lion", "den"),
    (r"(?:what|which).*home.*beaver", "dam"),
    (r"(?:what|which).*home.*squirrel", "tree"),
    (r"(?:what|which).*home.*bat", "cave"),
    (r"(?:what|which).*home.*penguin", "rookery"),
    (r"(?:what|which).*home.*eagle", "eyrie"),
    (r"(?:what|which).*where.*bird.*live", "nest"),
    (r"(?:what|which).*where.*bee.*live", "hive"),
    (r"(?:what|which).*where.*spider.*live", "web"),
    (r"(?:what|which).*where.*fish.*live", "water"),
    (r"(?:what|which).*where.*ant.*live", "anthill"),
    # -- Descriptions / What word describes X --
    (r"(?:what|which).*word.*describe.*dark", "dim"),
    (r"(?:what|which).*word.*describe.*bright", "luminous"),
    (r"(?:what|which).*word.*describe.*(?:hot|heat)", "warm"),
    (r"(?:what|which).*word.*describe.*cold", "chilly"),
    (r"(?:what|which).*word.*describe.*big", "large"),
    (r"(?:what|which).*word.*describe.*small", "tiny"),
    (r"(?:what|which).*word.*describe.*fast", "quick"),
    (r"(?:what|which).*word.*describe.*slow", "sluggish"),
    (r"(?:what|which).*word.*describe.*happy", "joyful"),
    (r"(?:what|which).*word.*describe.*sad", "melancholy"),
    (r"(?:what|which).*word.*describe.*angry", "furious"),
    (r"(?:what|which).*word.*describe.*scared", "frightened"),
    (r"(?:what|which).*word.*describe.*brave", "courageous"),
    (r"(?:what|which).*word.*describe.*beautiful", "gorgeous"),
    (r"(?:what|which).*word.*describe.*ugly", "hideous"),
    (r"(?:what|which).*word.*describe.*(?:tasty|delicious)", "scrumptious"),
    (r"(?:what|which).*word.*describe.*loud", "noisy"),
    (r"(?:what|which).*word.*describe.*quiet", "silent"),
    # -- Which does not belong / odd one out --
    (r"(?:what|which).*(?:does not belong|doesn't belong|odd one out|doesn't fit)", ""),
    # -- Geography: rivers, mountains, deserts --
    (r"(?:what|which).*(?:longest|largest).*river.*(?:world|earth)", "nile"),
    (r"(?:what|which).*(?:longest|largest).*river.*(?:south america|brazil)", "amazon"),
    (r"(?:what|which).*(?:longest|largest).*river.*usa|united states.*longest.*river", "mississippi"),
    (r"(?:what|which).*(?:highest|tallest).*mountain.*(?:world|earth)", "everest"),
    (r"(?:what|which).*(?:highest|tallest).*mountain.*africa", "kilimanjaro"),
    (r"(?:what|which).*(?:highest|tallest).*mountain.*north america", "denali"),
    (r"(?:what|which).*(?:highest|tallest).*mountain.*(?:europe|alps)", "mont blanc"),
    (r"(?:what|which).*(?:largest|biggest).*desert.*(?:world|earth)", "sahara"),
    (r"(?:what|which).*(?:coldest|driest).*desert", "antarctica"),
    (r"(?:what|which).*(?:largest|biggest).*ocean", "pacific"),
    (r"(?:what|which).*(?:smallest).*ocean", "arctic"),
    (r"(?:what|which).*(?:deepest).*ocean", "pacific"),
    (r"(?:what|which).*(?:largest|biggest).*lake.*(?:world|earth)", "caspian sea"),
    (r"(?:what|which).*(?:deepest).*lake", "baikal"),
    (r"(?:what|which).*(?:largest|biggest).*(?:country|nation).*area", "russia"),
    (r"(?:what|which).*(?:largest|biggest).*(?:country|nation).*population", "india"),
    (r"(?:what|which).*(?:smallest).*(?:country|nation)", "vatican city"),
    (r"(?:what|which).*(?:largest|biggest).*continent", "asia"),
    (r"(?:what|which).*(?:smallest).*continent", "australia"),
    # -- Which animal / classification --
    (r"(?:what|which).*animal.*(?:largest|biggest).*(?:land|earth)", "elephant"),
    (r"(?:what|which).*(?:largest|biggest).*animal.*(?:world|earth|ever)", "blue whale"),
    (r"(?:what|which).*animal.*(?:fastest|quickest).*(?:land|earth)", "cheetah"),
    (r"(?:what|which).*(?:fastest|quickest).*animal.*(?:world|earth|ever)", "peregrine falcon"),
    (r"(?:what|which).*animal.*(?:tallest|highest)", "giraffe"),
    (r"(?:what|which).*animal.*(?:longest|long) neck", "giraffe"),
    (r"(?:what|which).*animal.*(?:most.*poisonous|most.*venomous)", "box jellyfish"),
    (r"(?:what|which).*animal.*(?:can fly|flies|fly)", "bird"),
    (r"(?:what|which).*animal.*(?:live.*water|swims|swim)", "fish"),
    (r"(?:what|which).*animal.*(?:hop|jump).*pouch", "kangaroo"),
    (r"(?:what|which).*animal.*(?:trunk|tusks)", "elephant"),
    (r"(?:what|which).*animal.*(?:hump|desert.*live)", "camel"),
    (r"(?:what|which).*animal.*(?:stripes.*black.*white|black.*white.*stripes)", "zebra"),
    (r"(?:what|which).*animal.*(?:spots.*tall|tall.*spots)", "giraffe"),
    (r"(?:what|which).*animal.*(?:nocturnal|night.*awake)", "owl"),
    (r"(?:what|which).*animal.*(?:hibernate|sleep.*winter)", "bear"),
    (r"(?:what|which).*animal.*(?:change.*color|camouflage)", "chameleon"),
    (r"(?:what|which).*animal.*(?:black.*white.*china|china.*bear)", "panda"),
    (r"(?:what|which).*animal.*(?:king.*jungle|mane)", "lion"),
    (r"(?:what|which).*animal.*(?:lay.*egg.*milk|egg.*laying.*mammal)", "platypus"),
    # -- Plants / trees / flowers --
    (r"(?:what|which).*(?:tallest|highest).*tree", "redwood"),
    (r"(?:what|which).*tree.*(?:longest|oldest).*live|living.*longest.*tree", "bristlecone pine"),
    (r"(?:what|which).*flower.*love|love.*flower", "rose"),
    (r"(?:what|which).*flower.*sun|sun.*flower", "sunflower"),
    (r"(?:what|which).*flower.*(?:netherlands|holland|dutch)", "tulip"),
    (r"(?:what|which).*flower.*japan|japan.*flower", "cherry blossom"),
    (r"(?:what|which).*plant.*desert|desert.*plant|succulent.*plant", "cactus"),
    (r"(?:what|which).*plant.*eat.*insect|carnivor.*plant", "venus flytrap"),
    (r"(?:what|which).*fastest.*grow.*plant", "bamboo"),
    # -- Weather / climate --
    (r"(?:what|which).*(?:type|kind).*storm.*(?:spin|rotate|funnel)", "tornado"),
    (r"(?:what|which).*storm.*(?:ocean|sea|tropical).*rain", "hurricane"),
    (r"(?:what|which).*frozen.*rain|rain.*freeze", "hail"),
    (r"(?:what|which).*(?:white|fluffy).*fall.*sky.*cold", "snow"),
    (r"(?:what|which).*(?:arc|bow).*color.*sky.*(?:rain|after.*rain)", "rainbow"),
    (r"(?:what|which).*light.*flash.*sky.*thunder", "lightning"),
    (r"(?:what|which).*sound.*thunder|thunder.*sound", "thunder"),
    (r"(?:what|which).*temperature.*water.*boil|boil.*water.*temperature", "100"),
    (r"(?:what|which).*temperature.*water.*(?:freeze|ice)|water.*freeze.*temperature", "0"),
    (r"(?:what|which).*(?:scale|unit).*temperature.*usa", "fahrenheit"),
    (r"(?:what|which).*(?:scale|unit).*temperature.*(?:world|most|metric)", "celsius"),
    # -- Inventions / Who invented --
    (r"(?:what|which).*invent.*telephone|who.*invent.*telephone|telephone.*invent", "alexander graham bell"),
    (r"(?:what|which).*invent.*(?:light bulb|lightbulb)|who.*invent.*light.*bulb", "thomas edison"),
    (r"(?:what|which).*invent.*radio|who.*invent.*radio", "guglielmo marconi"),
    (r"(?:what|which).*invent.*airplane|who.*invent.*airplane", "wright brothers"),
    (r"(?:what|which).*invent.*car|who.*invent.*automobile", "karl benz"),
    (r"(?:what|which).*invent.*penicillin|who.*discover.*penicillin", "alexander fleming"),
    (r"(?:what|which).*invent.*world wide web|who.*invent.*www", "tim berners-lee"),
    (r"(?:what|which).*invent.*dynamite|who.*invent.*dynamite", "alfred nobel"),
    (r"(?:what|which).*invent.*printing press|who.*invent.*printing", "johannes gutenberg"),
    (r"(?:what|which).*invent.*steam engine|who.*invent.*steam", "james watt"),
    (r"(?:what|which).*invent.*tesla coil|who.*invent.*tesla coil", "nikola tesla"),
    (r"(?:what|which).*discover.*gravity|who.*discover.*gravity", "isaac newton"),
    (r"(?:what|which).*discover.*america|who.*discover.*america", "christopher columbus"),
    (r"(?:what|which).*paint.*mona lisa|who.*paint.*mona lisa", "leonardo da vinci"),
    (r"(?:what|which).*write.*romeo.*juliet|who.*write.*romeo", "william shakespeare"),
    (r"(?:what|which).*write.*harry potter|who.*write.*harry potter", "j.k. rowling"),
    (r"(?:what|which).*compose.*fifth symphony|who.*compose.*fifth", "beethoven"),
    (r"(?:what|which).*compose.*moonlight sonata|who.*compose.*moonlight", "beethoven"),
    # -- Computer / tech --
    (r"(?:what|which).*primary.*input.*device.*computer", "keyboard"),
    (r"(?:what|which).*pointing.*device.*computer", "mouse"),
    (r"(?:what|which).*display.*screen.*computer", "monitor"),
    (r"(?:what|which).*brain.*computer", "cpu"),
    (r"(?:what|which).*temporary.*memory.*computer", "ram"),
    (r"(?:what|which).*permanent.*storage.*computer", "hard drive"),
    (r"(?:what|which).*operating.*system.*(?:apple|mac)", "macos"),
    (r"(?:what|which).*operating.*system.*microsoft", "windows"),
    (r"(?:what|which).*operating.*system.*(?:open source|linux|free)", "linux"),
    (r"(?:what|which).*programming.*(?:language|lang).*(?:web|internet|browser)", "javascript"),
    (r"(?:what|which).*browser.*google", "chrome"),
    (r"(?:what|which).*search.*engine.*(?:popular|most.*use|biggest)", "google"),
    (r"(?:what|which).*social.*media.*(?:most.*use|popular|biggest)", "facebook"),
    (r"(?:what|which).*video.*platform.*(?:most.*use|popular)", "youtube"),
    # -- Art / colors --
    (r"(?:what|which).*primary.*color.*(?:red.*yellow.*blue|paint|pigment)", "red"),
    (r"(?:what|which).*primary.*color.*light.*(?:red.*green.*blue|rgb)", "red"),
    (r"(?:what|which).*mix.*red.*(?:yellow|blue).*get", ""),
    (r"(?:what|which).*color.*mix.*red.*blue", "purple"),
    (r"(?:what|which).*color.*mix.*red.*yellow", "orange"),
    (r"(?:what|which).*color.*mix.*blue.*yellow", "green"),
    (r"(?:what|which).*color.*mix.*red.*white", "pink"),
    (r"(?:what|which).*color.*mix.*black.*white", "gray"),
    (r"(?:what|which).*(?:warm|hot).*color", "red"),
    (r"(?:what|which).*(?:cool|cold).*color", "blue"),
    # -- Famous quotes / sayings --
    (r"(?:what|which).*i think.*therefore.*am|think.*therefore.*am", "descartes"),
    (r"(?:what|which).*to be or not to be", "hamlet"),
    (r"(?:what|which).*one small step.*man.*giant leap|one small step", "neil armstrong"),
    (r"(?:what|which).*i have a dream.*speech", "martin luther king"),
    (r"(?:what|which).*eureka.*discover|bathtub.*discover", "archimedes"),
    (r"(?:what|which).*let them eat cake", "marie antoinette"),
    # -- Vehicles / transport --
    (r"(?:what|which).*vehicle.*(?:fly|air|sky|wing)", "airplane"),
    (r"(?:what|which).*vehicle.*(?:water|sea|ocean|boat|ship)", "boat"),
    (r"(?:what|which).*vehicle.*(?:road|street|drive|car)", "car"),
    (r"(?:what|which).*vehicle.*(?:rail|track|train)", "train"),
    (r"(?:what|which).*vehicle.*(?:space|rocket|moon)", "spaceship"),
    (r"(?:what|which).*vehicle.*(?:underwater|submarine|dive)", "submarine"),
    (r"(?:what|which).*vehicle.*(?:two wheel|bike|bicycle)", "bicycle"),
    (r"(?:what|which).*(?:fly|air|sky|wing).*vehicle", "airplane"),
    (r"(?:what|which).*(?:sail|float).*vehicle", "boat"),
    # -- Body parts / anatomy --
    (r"(?:what|which).*pump.*blood.*body", "heart"),
    (r"(?:what|which).*organ.*(?:think|thought|brain)", "brain"),
    (r"(?:what|which).*organ.*(?:breath|breathe|lungs|oxygen)", "lungs"),
    (r"(?:what|which).*organ.*(?:filter.*blood|filter.*toxin)", "liver"),
    (r"(?:what|which).*organ.*(?:digest|stomach|food.*break)", "stomach"),
    (r"(?:what|which).*joint.*(?:knee|leg.*bend)", "knee"),
    (r"(?:what|which).*joint.*(?:elbow|arm.*bend)", "elbow"),
    (r"(?:what|which).*(?:largest|biggest).*organ.*body", "skin"),
    (r"(?:what|which).*(?:smallest).*bone.*body", "stapes"),
    (r"(?:what|which).*(?:longest|largest).*bone.*body", "femur"),
    (r"(?:what|which).*strongest.*muscle", "tongue"),
    (r"(?:what|which).*hardest.*substance.*body", "enamel"),
    # -- Time / calendar --
    (r"(?:what|which).*(?:first|1st).*day.*week", "sunday"),
    (r"(?:what|which).*(?:last|7th).*day.*week", "saturday"),
    (r"(?:what|which).*(?:first|1st).*month.*year", "january"),
    (r"(?:what|which).*(?:last|12th).*month.*year", "december"),
    (r"(?:what|which).*month.*(?:shortest|least.*day)", "february"),
    (r"(?:what|which).*month.*(?:longest|most.*day)", "july"),
    (r"(?:what|which).*day.*leap year|leap year.*day", "february 29"),
    (r"(?:what|which).*year.*leap year|leap year.*how often", "4"),
    (r"(?:what|which).*season.*after.*winter", "spring"),
    (r"(?:what|which).*season.*after.*spring", "summer"),
    (r"(?:what|which).*season.*after.*summer", "autumn"),
    (r"(?:what|which).*season.*after.*(?:autumn|fall)", "winter"),
    # -- Food / cooking --
    (r"(?:what|which).*fruit.*(?:king|king.*fruit)", "durian"),
    (r"(?:what|which).*fruit.*(?:queen|queen.*fruit)", "mangosteen"),
    (r"(?:what|which).*most.*consumed.*fruit.*world", "banana"),
    (r"(?:what|which).*most.*consumed.*vegetable.*world", "tomato"),
    (r"(?:what|which).*most.*consumed.*meat.*world", "pork"),
    (r"(?:what|which).*most.*drink.*beverage.*world.*water|most.*consumed.*drink", "water"),
    (r"(?:what|which).*most.*popular.*hot.*drink", "coffee"),
    (r"(?:what|which).*most.*popular.*alcohol", "beer"),
    (r"(?:what|which).*spiciest.*pepper", "carolina reaper"),
    (r"(?:what|which).*(?:raw|uncooked).*sushi.*fish", "sashimi"),
    (r"(?:what|which).*italian.*dish.*pasta.*meat.*sauce", "spaghetti bolognese"),
    (r"(?:what|which).*italian.*dish.*pizza.*cheese.*tomato", "margherita"),
    (r"(?:what|which).*japanese.*dish.*raw.*fish.*rice", "sushi"),
    (r"(?:what|which).*mexican.*dish.*tortilla.*meat.*cheese", "taco"),
    # -- Sports --
    (r"(?:what|which).*sport.*(?:bat.*ball|baseball)", "baseball"),
    (r"(?:what|which).*sport.*(?:racket.*net|tennis)", "tennis"),
    (r"(?:what|which).*sport.*(?:goal.*net.*no.*hand|soccer)", "soccer"),
    (r"(?:what|which).*sport.*(?:touchdown|football.*american)", "football"),
    (r"(?:what|which).*sport.*(?:basket.*hoop|basketball)", "basketball"),
    (r"(?:what|which).*sport.*(?:ice.*puck|hockey)", "hockey"),
    (r"(?:what|which).*sport.*(?:swim.*pool|swimming)", "swimming"),
    (r"(?:what|which).*sport.*(?:golf.*club.*hole)", "golf"),
    (r"(?:what|which).*sport.*(?:bowling.*pin.*ball)", "bowling"),
    (r"(?:what|which).*sport.*(?:volleyball.*net.*beach)", "volleyball"),
    (r"(?:what|which).*sport.*(?:boxing.*glove.*punch)", "boxing"),
    (r"(?:what|which).*sport.*(?:wrestl|sumo)", "wrestling"),
    # -- Music instruments --
    (r"(?:what|which).*instrument.*(?:piano.*key|string.*hammer|keyboard.*musical)", "piano"),
    (r"(?:what|which).*instrument.*(?:guitar.*string.*pluck)", "guitar"),
    (r"(?:what|which).*instrument.*(?:violin.*bow.*string)", "violin"),
    (r"(?:what|which).*instrument.*(?:drum.*hit.*percussion)", "drum"),
    (r"(?:what|which).*instrument.*(?:flute.*blow.*wind)", "flute"),
    (r"(?:what|which).*instrument.*(?:trumpet.*brass.*blow)", "trumpet"),
    (r"(?:what|which).*instrument.*(?:saxophone.*jazz.*reed)", "saxophone"),
    (r"(?:what|which).*instrument.*(?:harp.*string.*pluck.*big)", "harp"),
    (r"(?:what|which).*instrument.*(?:largest|biggest).*orchestra", "double bass"),
    (r"(?:what|which).*instrument.*(?:smallest|highest).*orchestra", "piccolo"),
    # -- Measurements / units --
    (r"(?:what|which).*unit.*(?:length|distance).*metric|metric.*unit.*length", "meter"),
    (r"(?:what|which).*unit.*(?:weight|mass).*metric|metric.*unit.*weight", "kilogram"),
    (r"(?:what|which).*unit.*(?:volume|liquid).*metric|metric.*unit.*volume", "liter"),
    (r"(?:what|which).*unit.*(?:length|distance).*imperial|imperial.*unit.*length", "inch"),
    (r"(?:what|which).*unit.*(?:weight|mass).*imperial|imperial.*unit.*weight", "pound"),
    (r"(?:what|which).*unit.*(?:volume|liquid).*imperial|imperial.*unit.*volume", "gallon"),
    # -- Random catch-all --
    (r"(?:what|which).*(?:drink|beverage).*(?:caffeine|coffee.*bean|wake.*up)", "coffee"),
    (r"(?:what|which).*(?:drink|beverage).*leaf.*(?:hot|green|black)", "tea"),
    (r"(?:what|which).*(?:fruit|food).*(?:monkey|banana)", "banana"),
    (r"(?:what|which).*bird.*(?:can't fly|flightless).*(?:big|large|tall)", "ostrich"),
    (r"(?:what|which).*bird.*(?:can't fly|flightless).*cold", "penguin"),
    (r"(?:what|which).*bird.*(?:can't fly|flightless).*new zealand", "kiwi"),
    (r"(?:what|which).*bird.*(?:symbol.*peace|peace.*symbol|white.*dove)", "dove"),
    (r"(?:what|which).*bird.*(?:symbol.*usa|united.*states.*bird|bald)", "bald eagle"),
    (r"(?:what|which).*animal.*(?:symbol.*(?:australia|aussie))", "kangaroo"),
    (r"(?:what|which).*animal.*(?:symbol.*canada)", "beaver"),
    (r"(?:what|which).*animal.*(?:symbol.*china)", "panda"),
    (r"(?:what|which).*animal.*(?:symbol.*india)", "tiger"),
    (r"(?:what|which).*animal.*(?:symbol.*russia)", "bear"),
    (r"(?:what|which).*language.*(?:most.*spoken|most.*speaker)", "mandarin"),
    (r"(?:what|which).*language.*(?:most.*country|most.*official)", "english"),
    (r"(?:what|which).*language.*(?:oldest.*still.*used|oldest.*living)", "tamil"),
    (r"(?:what|which).*city.*(?:most.*population|largest.*city|biggest.*city)", "tokyo"),
    (r"(?:what|which).*city.*(?:never.*sleep|big apple)", "new york"),
    (r"(?:what|which).*city.*(?:city.*light|eiffel.*tower)", "paris"),
    (r"(?:what|which).*city.*(?:canal.*gondola)", "venice"),
    (r"(?:what|which).*city.*(?:ancient.*rome|colosseum|roman.*empire)", "rome"),
    (r"(?:what|which).*city.*(?:hollywood|movie.*capital)", "los angeles"),
    (r"(?:what|which).*city.*(?:financial.*capital.*world|wall.*street)", "new york"),
    # -- Which planet --
    (r"(?:what|which).*planet.*(?:closest|nearest).*sun", "mercury"),
    (r"(?:what|which).*planet.*(?:farthest|furthest).*sun", "neptune"),
    (r"(?:what|which).*planet.*(?:largest|biggest)", "jupiter"),
    (r"(?:what|which).*planet.*(?:smallest)", "mercury"),
    (r"(?:what|which).*planet.*(?:hottest)", "venus"),
    (r"(?:what|which).*planet.*(?:coldest)", "neptune"),
    (r"(?:what|which).*planet.*(?:ring|rings)", "saturn"),
    (r"(?:what|which).*planet.*(?:red|reddish)", "mars"),
    (r"(?:what|which).*planet.*(?:blue|water)", "earth"),
    (r"(?:what|which).*planet.*(?:life|alive|living)", "earth"),
    (r"(?:what|which).*planet.*(?:storm.*big|great red spot)", "jupiter"),
    # -- Chemical elements --
    (r"(?:what|which).*element.*(?:most.*abundant.*universe)", "hydrogen"),
    (r"(?:what|which).*element.*(?:most.*abundant.*earth.*crust)", "oxygen"),
    (r"(?:what|which).*element.*(?:lightest|lightest.*gas)", "hydrogen"),
    (r"(?:what|which).*element.*(?:heaviest.*natural)", "uranium"),
    (r"(?:what|which).*element.*(?:liquid.*room.*temperature)", "mercury"),
    (r"(?:what|which).*element.*(?:noble.*gas.*yellow.*sign)", "neon"),
    (r"(?:what|which).*element.*(?:gold|au)", "gold"),
    (r"(?:what|which).*element.*(?:silver|ag).*(?:metal)", "silver"),
    (r"(?:what|which).*element.*(?:diamond|hardest)", "carbon"),
    (r"(?:what|which).*gas.*(?:breathe|breath|survive).*(?!nitrogen|carbon)", "oxygen"),
    (r"(?:what|which).*gas.*(?:most.*abundant.*(?:air|atmosphere)|most.*air)", "nitrogen"),
    (r"(?:what|which).*gas.*(?:plant.*breath|photosynthesis)", "carbon dioxide"),
    # -- Numbers / constants --
    (r"(?:what|which).*(?:pi|value of pi).*(?:approximate|approx|about)", "3.14"),
    (r"(?:what|which).*(?:speed.*light|light.*speed)", "300000"),
    (r"(?:what|which).*(?:absolute zero).*(?:temperature|degrees)", "-273"),
    (r"(?:what|which).*(?:normal.*body.*temperature|body.*temp.*normal)", "37"),
    (r"(?:what|which).*(?:freezing.*point.*water.*celsius|water.*freeze.*celsius)", "0"),
    (r"(?:what|which).*(?:boiling.*point.*water.*celsius|water.*boil.*celsius)", "100"),
    (r"(?:what|which).*(?:freezing.*point.*water.*fahrenheit|water.*freeze.*fahrenheit)", "32"),
    (r"(?:what|which).*(?:boiling.*point.*water.*fahrenheit|water.*boil.*fahrenheit)", "212"),
    # -- What is the opposite / antonym (just in case homophone missed) --
    (r"(?:what|which).*opposite.*of.*hot", "cold"),
    (r"(?:what|which).*opposite.*of.*cold", "hot"),
    (r"(?:what|which).*opposite.*of.*big", "small"),
    (r"(?:what|which).*opposite.*of.*small", "big"),
    (r"(?:what|which).*opposite.*of.*fast", "slow"),
    (r"(?:what|which).*opposite.*of.*slow", "fast"),
    (r"(?:what|which).*opposite.*of.*light.*(?:heavy|weight)", "heavy"),
    (r"(?:what|which).*opposite.*of.*heavy", "light"),
    (r"(?:what|which).*opposite.*of.*hard", "soft"),
    (r"(?:what|which).*opposite.*of.*soft", "hard"),
    (r"(?:what|which).*opposite.*of.*happy", "sad"),
    (r"(?:what|which).*opposite.*of.*sad", "happy"),
    (r"(?:what|which).*opposite.*of.*rich", "poor"),
    (r"(?:what|which).*opposite.*of.*poor", "rich"),
    (r"(?:what|which).*opposite.*of.*young", "old"),
    (r"(?:what|which).*opposite.*of.*old", "young"),
    (r"(?:what|which).*opposite.*of.*open", "closed"),
    (r"(?:what|which).*opposite.*of.*(?:close|shut)", "open"),
    (r"(?:what|which).*opposite.*of.*(?:day|daytime)", "night"),
    (r"(?:what|which).*opposite.*of.*night", "day"),
    (r"(?:what|which).*opposite.*of.*(?:up|above)", "down"),
    (r"(?:what|which).*opposite.*of.*(?:down|below)", "up"),
    (r"(?:what|which).*opposite.*of.*left", "right"),
    (r"(?:what|which).*opposite.*of.*right", "left"),
    (r"(?:what|which).*opposite.*of.*(?:full|filled)", "empty"),
    (r"(?:what|which).*opposite.*of.*empty", "full"),
    (r"(?:what|which).*opposite.*of.*(?:male|man)", "female"),
    (r"(?:what|which).*opposite.*of.*(?:female|woman)", "male"),
    (r"(?:what|which).*opposite.*of.*(?:true|truth)", "false"),
    (r"(?:what|which).*opposite.*of.*(?:false|lie)", "true"),
    (r"(?:what|which).*opposite.*of.*(?:love|adore)", "hate"),
    (r"(?:what|which).*opposite.*of.*hate", "love"),
    (r"(?:what|which).*opposite.*of.*(?:begin|start)", "end"),
    (r"(?:what|which).*opposite.*of.*end", "begin"),
    (r"(?:what|which).*opposite.*of.*(?:give|donate)", "take"),
    (r"(?:what|which).*opposite.*of.*take", "give"),
    (r"(?:what|which).*opposite.*of.*(?:win|victory)", "lose"),
    (r"(?:what|which).*opposite.*of.*(?:lose|defeat)", "win"),
    (r"(?:what|which).*opposite.*of.*(?:strong|powerful)", "weak"),
    (r"(?:what|which).*opposite.*of.*(?:weak|feeble)", "strong"),
    (r"(?:what|which).*opposite.*of.*(?:wide|broad)", "narrow"),
    (r"(?:what|which).*opposite.*of.*(?:narrow|thin)", "wide"),
    (r"(?:what|which).*opposite.*of.*(?:deep|profound)", "shallow"),
    (r"(?:what|which).*opposite.*of.*(?:shallow|superficial)", "deep"),
    (r"(?:what|which).*opposite.*of.*(?:thick|dense)", "thin"),
    (r"(?:what|which).*opposite.*of.*(?:thin|skinny)", "thick"),
    (r"(?:what|which).*opposite.*of.*(?:tall|high)", "short"),
    (r"(?:what|which).*opposite.*of.*short", "tall"),
    (r"(?:what|which).*opposite.*of.*(?:safe|secure)", "dangerous"),
    (r"(?:what|which).*opposite.*of.*(?:dangerous|risky|unsafe)", "safe"),


    # -- Colors of things --
    (r"what color.*egg white", "white"),
    (r"what color.*egg yolk", "yellow"),
    (r"what color.*(?:sky|daytime sky)", "blue"),
    (r"what color.*night sky", "black"),
    (r"what color.*sun", "yellow"),
    (r"what color.*(?:grass|healthy grass)", "green"),
    (r"what color.*snow", "white"),
    (r"what color.*(?:cloud|clouds)", "white"),
    (r"what color.*(?:blood|rose|strawberry|cherry|ruby)", "red"),
    (r"what color.*(?:ocean|sea|sapphire|blueberry)", "blue"),
    (r"what color.*(?:lemon|banana|sunflower|gold)", "yellow"),
    (r"what color.*(?:orange fruit|carrot|pumpkin)", "orange"),
    (r"what color.*(?:chocolate|coffee|wood|mud)", "brown"),
    (r"what color.*(?:grape|eggplant|amethyst|lavender)", "purple"),
    (r"what color.*(?:leaf|leaves|lettuce|cucumber|emerald)", "green"),
    (r"what color.*(?:flamingo|pig|bubblegum)", "pink"),
    (r"what color.*(?:zebra|panda|penguin)", "black and white"),
    (r"what color.*(?:coal|charcoal|raven|crow)", "black"),
    (r"what color.*(?:milk|cotton|sugar|salt|chalk)", "white"),
    (r"what color.*(?:silver|ash|elephant|cement|steel|iron)", "gray"),
    (r"what color.*(?:autumn leaf|fall leaf|sunset)", "orange"),
    (r"what color.*ladybug", "red and black"),
    (r"what color.*bee", "yellow and black"),
    # -- Which is bigger / smaller / heavier / etc --
    (r"which.*bigger.*elephant.*mouse|which.*bigger.*mouse.*elephant", "elephant"),
    (r"which.*bigger.*whale.*shark|which.*bigger.*shark.*whale", "whale"),
    (r"which.*bigger.*sun.*earth|which.*bigger.*earth.*sun", "sun"),
    (r"which.*bigger.*earth.*moon|which.*bigger.*moon.*earth", "earth"),
    (r"which.*smaller.*mouse.*elephant|which.*smaller.*elephant.*mouse", "mouse"),
    (r"which.*heavier.*elephant.*mouse|which.*heavier.*mouse.*elephant", "elephant"),
    (r"which.*heavier.*rock.*feather|which.*heavier.*feather.*rock", "rock"),
    (r"which.*lighter.*feather.*rock|which.*lighter.*rock.*feather", "feather"),
    (r"which.*taller.*giraffe.*dog|which.*taller.*dog.*giraffe", "giraffe"),
    (r"which.*faster.*cheetah.*turtle|which.*faster.*turtle.*cheetah", "cheetah"),
    (r"which.*slower.*turtle.*cheetah|which.*slower.*cheetah.*turtle", "turtle"),
    (r"which.*hotter.*sun.*earth|which.*hotter.*earth.*sun", "sun"),
    (r"which.*hotter.*fire.*ice|which.*hotter.*ice.*fire", "fire"),
    (r"which.*colder.*ice.*fire|which.*colder.*fire.*ice", "ice"),
    (r"which.*older.*pyramid.*skyscraper|which.*older.*skyscraper.*pyramid", "pyramid"),
    # -- Type / kind / category --
    (r"what type.*animal.*(?:frog|toads)", "amphibian"),
    (r"what type.*animal.*(?:snake|lizard|turtle)", "reptile"),
    (r"what type.*animal.*(?:whale|dolphin|human|dog|cat|lion|bear)", "mammal"),
    (r"what type.*animal.*(?:salmon|tuna|shark|goldfish)", "fish"),
    (r"what type.*animal.*(?:eagle|hawk|owl|sparrow|robin|parrot)", "bird"),
    (r"what type.*animal.*(?:spider|scorpion)", "arachnid"),
    (r"what type.*animal.*(?:ant|bee|butterfly|beetle|mosquito)", "insect"),
    (r"what type.*(?:rose|daisy|tulip|sunflower|lily|orchid)", "flower"),
    (r"what type.*(?:oak|pine|maple|birch|willow|palm)", "tree"),
    (r"what type.*(?:diamond|ruby|sapphire|emerald|opal)", "gem"),
    (r"what type.*(?:gold|silver|iron|copper|aluminum)", "metal"),
    (r"what type.*(?:oxygen|nitrogen|hydrogen|helium)", "gas"),
    (r"what type.*(?:piano|guitar|violin|drum|flute|trumpet)", "instrument"),
    (r"what type.*(?:car|truck|bus|train|airplane|boat)", "vehicle"),
    (r"what type.*(?:apple|banana|orange|grape|mango|strawberry)", "fruit"),
    (r"what type.*(?:carrot|broccoli|spinach|lettuce|potato|onion)", "vegetable"),
    (r"what type.*mushroom", "fungus"),
    (r"what type.*(?:rice|wheat|oats|barley)", "grain"),
    # -- Baby / young animals --
    (r"(?:what|which).*young.*cat.*called", "kitten"),
    (r"(?:what|which).*baby.*cat.*called", "kitten"),
    (r"(?:what|which).*young.*dog.*called", "puppy"),
    (r"(?:what|which).*baby.*dog.*called", "puppy"),
    (r"(?:what|which).*young.*cow.*called", "calf"),
    (r"(?:what|which).*baby.*cow.*called", "calf"),
    (r"(?:what|which).*young.*horse.*called", "foal"),
    (r"(?:what|which).*baby.*horse.*called", "foal"),
    (r"(?:what|which).*young.*sheep.*called", "lamb"),
    (r"(?:what|which).*baby.*sheep.*called", "lamb"),
    (r"(?:what|which).*young.*goat.*called", "kid"),
    (r"(?:what|which).*baby.*goat.*called", "kid"),
    (r"(?:what|which).*young.*pig.*called", "piglet"),
    (r"(?:what|which).*baby.*pig.*called", "piglet"),
    (r"(?:what|which).*young.*bear.*called", "cub"),
    (r"(?:what|which).*baby.*bear.*called", "cub"),
    (r"(?:what|which).*young.*lion.*called", "cub"),
    (r"(?:what|which).*baby.*lion.*called", "cub"),
    (r"(?:what|which).*young.*tiger.*called", "cub"),
    (r"(?:what|which).*baby.*tiger.*called", "cub"),
    (r"(?:what|which).*young.*kangaroo.*called", "joey"),
    (r"(?:what|which).*baby.*kangaroo.*called", "joey"),
    (r"(?:what|which).*young.*frog.*called", "tadpole"),
    (r"(?:what|which).*baby.*frog.*called", "tadpole"),
    (r"(?:what|which).*young.*deer.*called", "fawn"),
    (r"(?:what|which).*baby.*deer.*called", "fawn"),
    (r"(?:what|which).*young.*duck.*called", "duckling"),
    (r"(?:what|which).*baby.*duck.*called", "duckling"),
    (r"(?:what|which).*young.*goose.*called", "gosling"),
    (r"(?:what|which).*baby.*goose.*called", "gosling"),
    (r"(?:what|which).*young.*swan.*called", "cygnet"),
    (r"(?:what|which).*baby.*swan.*called", "cygnet"),
    (r"(?:what|which).*young.*eagle.*called", "eaglet"),
    (r"(?:what|which).*baby.*eagle.*called", "eaglet"),
    (r"(?:what|which).*young.*owl.*called", "owlet"),
    (r"(?:what|which).*baby.*owl.*called", "owlet"),
    # -- Group / collective nouns --
    (r"(?:what|which).*group.*wolf.*called", "pack"),
    (r"(?:what|which).*group.*fish.*called", "school"),
    (r"(?:what|which).*group.*bird.*called", "flock"),
    (r"(?:what|which).*group.*sheep.*called", "flock"),
    (r"(?:what|which).*group.*cattle.*called", "herd"),
    (r"(?:what|which).*group.*lion.*called", "pride"),
    (r"(?:what|which).*group.*bee.*called", "swarm"),
    (r"(?:what|which).*group.*ant.*called", "colony"),
    (r"(?:what|which).*group.*dolphin.*called", "pod"),
    (r"(?:what|which).*group.*whale.*called", "pod"),
    (r"(?:what|which).*group.*geese.*called", "gaggle"),
    (r"(?:what|which).*group.*crow.*called", "murder"),
    (r"(?:what|which).*group.*owl.*called", "parliament"),
    # -- Male / female animals --
    (r"(?:what|which).*male.*chicken.*called", "rooster"),
    (r"(?:what|which).*female.*chicken.*called", "hen"),
    (r"(?:what|which).*male.*cow.*called", "bull"),
    (r"(?:what|which).*female.*cow.*called", "cow"),
    (r"(?:what|which).*male.*horse.*called", "stallion"),
    (r"(?:what|which).*female.*horse.*called", "mare"),
    (r"(?:what|which).*male.*sheep.*called", "ram"),
    (r"(?:what|which).*female.*sheep.*called", "ewe"),
    (r"(?:what|which).*male.*pig.*called", "boar"),
    (r"(?:what|which).*female.*pig.*called", "sow"),
    (r"(?:what|which).*male.*deer.*called", "buck"),
    (r"(?:what|which).*female.*deer.*called", "doe"),
    # -- "Used for" / purpose --
    (r"(?:what|which).*(?:use|used).*measur.*temperature", "thermometer"),
    (r"(?:what|which).*(?:use|used).*tell.*time", "clock"),
    (r"(?:what|which).*(?:use|used).*see.*far.*away", "telescope"),
    (r"(?:what|which).*(?:use|used).*see.*small.*thing", "microscope"),
    (r"(?:what|which).*(?:use|used).*cut.*paper", "scissors"),
    (r"(?:what|which).*(?:use|used).*write.*paper", "pen"),
    (r"(?:what|which).*(?:use|used).*erase.*pencil", "eraser"),
    (r"(?:what|which).*(?:use|used).*open.*lock", "key"),
    (r"(?:what|which).*(?:use|used).*open.*can", "can opener"),
    (r"(?:what|which).*(?:use|used).*open.*bottle", "bottle opener"),
    (r"(?:what|which).*(?:use|used).*open.*wine", "corkscrew"),
    (r"(?:what|which).*(?:use|used).*peel.*vegetable", "peeler"),
    (r"(?:what|which).*(?:use|used).*grate.*cheese", "grater"),
    (r"(?:what|which).*(?:use|used).*strain.*pasta", "colander"),
    (r"(?:what|which).*(?:use|used).*mix.*batter", "whisk"),
    (r"(?:what|which).*(?:use|used).*flip.*pancake", "spatula"),
    (r"(?:what|which).*(?:use|used).*serve.*soup", "ladle"),
    (r"(?:what|which).*(?:use|used).*measure.*(?:cup|ingredient|liquid)", "measuring cup"),
    (r"(?:what|which).*(?:use|used).*weigh.*(?:food|ingredient)", "scale"),
    (r"(?:what|which).*(?:use|used).*bake.*(?:cake|bread|cookie)", "oven"),
    (r"(?:what|which).*(?:use|used).*toast.*bread", "toaster"),
    (r"(?:what|which).*(?:use|used).*boil.*water", "kettle"),
    (r"(?:what|which).*(?:use|used).*brew.*coffee", "coffee maker"),
    (r"(?:what|which).*(?:use|used).*blend.*(?:smoothie|drink|liquid)", "blender"),
    (r"(?:what|which).*(?:use|used).*keep.*food.*cold", "refrigerator"),
    (r"(?:what|which).*(?:use|used).*freeze.*(?:food|ice)", "freezer"),
    (r"(?:what|which).*(?:use|used).*wash.*clothes", "washing machine"),
    (r"(?:what|which).*(?:use|used).*dry.*clothes", "dryer"),
    (r"(?:what|which).*(?:use|used).*clean.*carpet", "vacuum"),
    (r"(?:what|which).*(?:use|used).*iron.*clothes", "iron"),
    (r"(?:what|which).*(?:use|used).*cut.*hair", "scissors"),
    (r"(?:what|which).*(?:use|used).*cut.*wood", "saw"),
    (r"(?:what|which).*(?:use|used).*dig.*hole", "shovel"),
    (r"(?:what|which).*(?:use|used).*tighten.*screw", "screwdriver"),
    (r"(?:what|which).*(?:use|used).*pound.*nail", "hammer"),
    (r"(?:what|which).*(?:use|used).*measure.*length", "ruler"),
    (r"(?:what|which).*(?:use|used).*draw.*straight.*line", "ruler"),
    (r"(?:what|which).*(?:use|used).*cut.*fabric", "scissors"),
    (r"(?:what|which).*(?:use|used).*sew.*(?:cloth|fabric)", "needle"),
    (r"(?:what|which).*(?:use|used).*protect.*rain", "umbrella"),
    (r"(?:what|which).*(?:use|used).*protect.*sun", "sunscreen"),
    (r"(?:what|which).*(?:use|used).*protect.*head", "helmet"),
    (r"(?:what|which).*(?:use|used).*brush.*teeth", "toothbrush"),
    (r"(?:what|which).*(?:use|used).*brush.*hair", "hairbrush"),
    (r"(?:what|which).*(?:use|used).*dry.*hair", "hair dryer"),
    (r"(?:what|which).*(?:use|used).*shave.*(?:face|beard)", "razor"),
    (r"(?:what|which).*(?:use|used).*trim.*nail", "nail clippers"),
    (r"(?:what|which).*(?:use|used).*pluck.*eyebrow", "tweezers"),
    (r"(?:what|which).*(?:use|used).*carry.*(?:baby|infant)", "stroller"),
    (r"(?:what|which).*(?:use|used).*carry.*groceries", "bag"),
    (r"(?:what|which).*(?:use|used).*light.*candle", "match"),
    (r"(?:what|which).*(?:use|used).*attach.*paper", "stapler"),
    (r"(?:what|which).*(?:use|used).*remove.*staple", "staple remover"),
    (r"(?:what|which).*(?:use|used).*punch.*hole.*paper", "hole punch"),
    (r"(?:what|which).*(?:use|used).*highlight.*text", "highlighter"),
    (r"(?:what|which).*(?:use|used).*stick.*paper", "glue"),
    (r"(?:what|which).*(?:use|used).*clip.*paper", "paper clip"),
    (r"(?:what|which).*(?:use|used).*fasten.*paper", "stapler"),
    # -- What holds / contains --
    (r"(?:what|which).*(?:hold|contain|container).*water.*drink", "glass"),
    (r"(?:what|which).*(?:hold|contain|container).*hot.*drink", "mug"),
    (r"(?:what|which).*(?:hold|contain|container).*flowers", "vase"),
    (r"(?:what|which).*(?:hold|contain|container).*trash", "trash can"),
    (r"(?:what|which).*(?:hold|contain|container).*food.*lunch", "lunchbox"),
    (r"(?:what|which).*(?:hold|contain|container).*money.*pig", "piggy bank"),
    (r"(?:what|which).*(?:hold|contain|container).*(?:coin|money)", "wallet"),
    (r"(?:what|which).*(?:hold|contain|container).*book", "bookshelf"),
    (r"(?:what|which).*(?:hold|contain|container).*clothes", "closet"),
    (r"(?:what|which).*(?:hold|contain|container).*food.*cold", "refrigerator"),
    (r"(?:what|which).*(?:hold|contain|container).*pencil", "pencil case"),
    (r"(?:what|which).*(?:hold|contain|container).*toothbrush", "toothbrush holder"),
    (r"(?:what|which).*(?:hold|contain|container).*soap", "soap dish"),
    # -- Rooms in a house --
    (r"(?:what|which).*room.*(?:cook|stove|oven|kitchen)", "kitchen"),
    (r"(?:what|which).*room.*(?:sleep|bed|bedroom)", "bedroom"),
    (r"(?:what|which).*room.*(?:bath|shower|toilet|bathroom)", "bathroom"),
    (r"(?:what|which).*room.*(?:watch.*tv|television|living)", "living room"),
    (r"(?:what|which).*room.*(?:eat|dining|dinner)", "dining room"),
    (r"(?:what|which).*room.*(?:wash.*clothes|laundry)", "laundry room"),
    (r"(?:what|which).*room.*(?:work.*desk|office|study)", "office"),
    (r"(?:what|which).*room.*(?:car|garage)", "garage"),
    (r"(?:what|which).*room.*(?:store|storage|basement)", "basement"),
    (r"(?:what|which).*room.*(?:attic|roof.*store)", "attic"),
    # -- Jobs / professions --
    (r"(?:what|which).*person.*(?:fly.*plane|pilot)", "pilot"),
    (r"(?:what|which).*person.*(?:cook.*restaurant|chef)", "chef"),
    (r"(?:what|which).*person.*(?:put.*out.*fire|fire.*fighter)", "firefighter"),
    (r"(?:what|which).*person.*(?:catch.*criminal|police.*officer)", "police officer"),
    (r"(?:what|which).*person.*(?:teach.*school|teacher)", "teacher"),
    (r"(?:what|which).*person.*(?:treat.*sick|doctor)", "doctor"),
    (r"(?:what|which).*person.*(?:design.*building|architect)", "architect"),
    (r"(?:what|which).*person.*(?:cut.*hair.*profession|barber)", "barber"),
    (r"(?:what|which).*person.*(?:bake.*bread|cake.*profession)", "baker"),
    (r"(?:what|which).*person.*(?:farm.*crop|farmer)", "farmer"),
    (r"(?:what|which).*person.*(?:fix.*car|mechanic)", "mechanic"),
    (r"(?:what|which).*person.*(?:fix.*pipe|plumber)", "plumber"),
    (r"(?:what|which).*person.*(?:wire.*electric|electrician)", "electrician"),
    (r"(?:what|which).*person.*(?:paint.*(?:house|wall).*profession)", "painter"),
    (r"(?:what|which).*person.*(?:build.*house|carpenter)", "carpenter"),
    (r"(?:what|which).*person.*(?:take.*photo.*profession|photographer)", "photographer"),
    (r"(?:what|which).*person.*(?:sing.*profession|singer)", "singer"),
    (r"(?:what|which).*person.*(?:act.*movie.*profession|actor)", "actor"),
    (r"(?:what|which).*person.*(?:write.*book.*profession|author)", "author"),
    (r"(?:what|which).*person.*(?:paint.*art.*profession|artist)", "artist"),
    (r"(?:what|which).*person.*(?:play.*sport.*profession|athlete)", "athlete"),
    (r"(?:what|which).*person.*(?:defend.*court|lawyer)", "lawyer"),
    (r"(?:what|which).*person.*(?:make.*(?:decision|court).*judge)", "judge"),
    (r"(?:what|which).*person.*(?:deliver.*mail|mailman|postman)", "mail carrier"),
    (r"(?:what|which).*person.*(?:sell.*house|real estate|realtor)", "real estate agent"),
    (r"(?:what|which).*person.*(?:drive.*bus", "bus driver"),
    (r"(?:what|which).*person.*(?:drive.*taxi", "taxi driver"),
    (r"(?:what|which).*person.*(?:serve.*food.*restaurant|waiter)", "waiter"),
    (r"(?:what|which).*person.*(?:nurse.*hospital|nurse)", "nurse"),
    (r"(?:what|which).*person.*(?:care.*teeth|dentist)", "dentist"),
    (r"(?:what|which).*person.*(?:care.*eye.*vision|optometrist)", "optometrist"),
    (r"(?:what|which).*person.*(?:take.*care.*animal.*sick|veterinarian)", "veterinarian"),
    (r"(?:what|which).*person.*(?:study.*space|astronomer)", "astronomer"),
    (r"(?:what|which).*person.*(?:travel.*space|astronaut)", "astronaut"),
    (r"(?:what|which).*person.*(?:study.*weather|meteorologist)", "meteorologist"),
    (r"(?:what|which).*person.*(?:study.*(?:ancient|past).*dig|archaeologist)", "archaeologist"),
    (r"(?:what|which).*person.*(?:study.*science|scientist)", "scientist"),
    (r"(?:what|which).*person.*(?:help.*library|librarian)", "librarian"),
    # -- "What language do people speak in X" (redundant safety) --
    (r"(?:what|which).*language.*speak.*france", "french"),
    (r"(?:what|which).*language.*speak.*germany", "german"),
    (r"(?:what|which).*language.*speak.*italy", "italian"),
    (r"(?:what|which).*language.*speak.*(?:russia|russian)", "russian"),
    (r"(?:what|which).*language.*speak.*china", "mandarin"),
    (r"(?:what|which).*language.*speak.*japan", "japanese"),
    (r"(?:what|which).*language.*speak.*korea", "korean"),
    (r"(?:what|which).*language.*speak.*brazil", "portuguese"),
    (r"(?:what|which).*language.*speak.*(?:portugal|portuguese)", "portuguese"),
    (r"(?:what|which).*language.*speak.*(?:netherlands|holland|dutch)", "dutch"),
    (r"(?:what|which).*language.*speak.*greece", "greek"),
    (r"(?:what|which).*language.*speak.*turkey", "turkish"),
    (r"(?:what|which).*language.*speak.*poland", "polish"),
    (r"(?:what|which).*language.*speak.*(?:sweden|swedish)", "swedish"),
    (r"(?:what|which).*language.*speak.*(?:norway|norwegian)", "norwegian"),
    (r"(?:what|which).*language.*speak.*(?:denmark|danish)", "danish"),
    (r"(?:what|which).*language.*speak.*(?:finland|finnish)", "finnish"),
    (r"(?:what|which).*language.*speak.*(?:thailand|thai)", "thai"),
    (r"(?:what|which).*language.*speak.*(?:vietnam|vietnamese)", "vietnamese"),
    (r"(?:what|which).*language.*speak.*(?:india|hindi)", "hindi"),
    (r"(?:what|which).*language.*speak.*(?:arabic|saudi|egypt|arab)", "arabic"),
    # -- Body parts - what does X do --
    (r"(?:what|which).*organ.*(?:pump|circulat).*blood", "heart"),
    (r"(?:what|which).*organ.*(?:think|thought|reason|mind)", "brain"),
    (r"(?:what|which).*organ.*(?:breath|breathe|respir|oxygen)", "lungs"),
    (r"(?:what|which).*organ.*(?:digest|stomach.*break)", "stomach"),
    (r"(?:what|which).*organ.*(?:filter.*blood|detox|liver)", "liver"),
    (r"(?:what|which).*organ.*(?:filter.*waste|urine|kidney)", "kidney"),
    (r"(?:what|which).*(?:part|organ).*see|sense.*sight", "eye"),
    (r"(?:what|which).*(?:part|organ).*hear|sense.*hear", "ear"),
    (r"(?:what|which).*(?:part|organ).*smell|sense.*smell", "nose"),
    (r"(?:what|which).*(?:part|organ).*taste|sense.*taste", "tongue"),
    (r"(?:what|which).*(?:part|organ).*touch|sense.*touch", "skin"),
    (r"(?:what|which).*bone.*protect.*brain", "skull"),
    (r"(?:what|which).*bone.*protect.*heart.*lung", "ribcage"),
    (r"(?:what|which).*muscle.*(?:chewing|chew|jaw)", "masseter"),
    # -- Countries / capitals (more) --
    (r"(?:what|which).*capital.*(?:afghanistan|kabul)", "kabul"),
    (r"(?:what|which).*capital.*(?:iraq|baghdad)", "baghdad"),
    (r"(?:what|which).*capital.*(?:iran|tehran)", "tehran"),
    (r"(?:what|which).*capital.*(?:pakistan|islamabad)", "islamabad"),
    (r"(?:what|which).*capital.*(?:bangladesh|dhaka)", "dhaka"),
    (r"(?:what|which).*capital.*(?:nigeria|abuja)", "abuja"),
    (r"(?:what|which).*capital.*(?:ethiopia|addis ababa)", "addis ababa"),
    (r"(?:what|which).*capital.*(?:kenya|nairobi)", "nairobi"),
    (r"(?:what|which).*capital.*(?:ghana|accra)", "accra"),
    (r"(?:what|which).*capital.*(?:morocco|rabat)", "rabat"),
    (r"(?:what|which).*capital.*(?:peru|lima)", "lima"),
    (r"(?:what|which).*capital.*(?:chile|santiago)", "santiago"),
    (r"(?:what|which).*capital.*(?:colombia|bogota)", "bogota"),
    (r"(?:what|which).*capital.*(?:venezuela|caracas)", "caracas"),
    (r"(?:what|which).*capital.*(?:(?:new zealand|nz)|wellington)", "wellington"),
    (r"(?:what|which).*capital.*(?:philippines|manila)", "manila"),
    (r"(?:what|which).*capital.*(?:malaysia|kuala lumpur)", "kuala lumpur"),
    (r"(?:what|which).*capital.*(?:singapore)", "singapore"),
    (r"(?:what|which).*capital.*(?:indonesia|jakarta)", "jakarta"),
    (r"(?:what|which).*capital.*(?:ukraine|kyiv|kiev)", "kyiv"),
    (r"(?:what|which).*capital.*(?:switzerland|bern)", "bern"),
    (r"(?:what|which).*capital.*(?:austria|vienna)", "vienna"),
    (r"(?:what|which).*capital.*(?:hungary|budapest)", "budapest"),
    (r"(?:what|which).*capital.*(?:czech|prague)", "prague"),
    (r"(?:what|which).*capital.*(?:belgium|brussels)", "brussels"),
    (r"(?:what|which).*capital.*(?:ireland|dublin)", "dublin"),
    (r"(?:what|which).*capital.*(?:scotland|edinburgh)", "edinburgh"),
    (r"(?:what|which).*capital.*(?:portugal|lisbon)", "lisbon"),
    # -- "What number" questions --
    (r"(?:what|which).*number.*(?:comes after|follows|after.*comes).*(\d+)", ""),
    (r"(?:what|which).*number.*(?:comes before|precedes|before.*comes).*(\d+)", ""),
    (r"(?:what|which).*number.*between.*(\d+).*and.*(\d+)", ""),
    # -- More random quick answers --
    (r"what.*(?:drink|beverage).*(?:popular|common).*world.*water", "water"),
    (r"what.*(?:natural satellite|moon).*earth", "moon"),
    (r"what.*(?:star|closest star).*earth", "sun"),
    (r"what.*galaxy.*(?:earth|milky way|we.*live)", "milky way"),
    (r"what.*(?:shape|geometry).*3.*side", "triangle"),
    (r"what.*(?:shape|geometry).*4.*equal.*side", "square"),
    (r"what.*(?:shape|geometry).*no.*side.*no.*corner", "circle"),
    (r"what.*(?:shape|geometry).*3.*dimension|3d.*sphere", "sphere"),
    (r"what.*(?:shape|geometry).*3.*dimension|3d.*cube", "cube"),
    (r"what.*(?:shape|geometry).*3.*dimension|3d.*cylinder", "cylinder"),
    # -- "What does X mean / stand for" --
    (r"what.*(?:nasa).*stand.*for", "national aeronautics and space administration"),
    (r"what.*(?:fbi).*stand.*for", "federal bureau of investigation"),
    (r"what.*(?:cia).*stand.*for", "central intelligence agency"),
    (r"what.*(?:who).*stand.*for.*health", "world health organization"),
    (r"what.*(?:nato).*stand.*for", "north atlantic treaty organization"),
    (r"what.*(?:unesco).*stand.*for", "united nations educational scientific and cultural organization"),
    (r"what.*(?:unicef).*stand.*for", "united nations children's fund"),
    (r"what.*(?:lol|lol).*stand.*for", "laugh out loud"),
    (r"what.*(?:gps|gps).*stand.*for", "global positioning system"),
    (r"what.*(?:atm|atm).*stand.*for", "automated teller machine"),
    (r"what.*(?:pin|pin).*stand.*for", "personal identification number"),
    (r"what.*(?:url|url).*stand.*for", "uniform resource locator"),
    (r"what.*(?:html|html).*stand.*for", "hypertext markup language"),
    (r"what.*(?:http|http).*stand.*for", "hypertext transfer protocol"),
    (r"what.*(?:dna|dna).*stand.*for", "deoxyribonucleic acid"),
    (r"what.*(?:rna|rna).*stand.*for", "ribonucleic acid"),
    (r"what.*(?:laser|laser).*stand.*for", "light amplification by stimulated emission of radiation"),
    (r"what.*(?:radar|radar).*stand.*for", "radio detection and ranging"),
    (r"what.*(?:sonar|sonar).*stand.*for", "sound navigation and ranging"),
    (r"what.*(?:scuba).*stand.*for", "self contained underwater breathing apparatus"),
    # -- "How many X in Y" faster catch --
    (r"how many(?!.*\b31\b).*(?:day|days).*year", "365"),
    (r"how many.*(?:day|days).*leap year", "366"),
    (r"how many.*(?:week|weeks).*year", "52"),
    (r"how many.*(?:month|months)(?!.*\b31\b).*year", "12"),
    (r"how many.*(?:day|days).*week", "7"),
    (r"how many.*(?:hour|hours).*day", "24"),
    (r"how many.*(?:minute|minutes).*hour", "60"),
    (r"how many.*(?:second|seconds).*minute", "60"),
    (r"how many.*(?:continent|continents).*(?:world|earth|planet)", "7"),
    (r"how many.*(?:ocean|oceans).*(?:world|earth|planet)", "5"),
    (r"how many.*(?:planet|planets).*(?:solar)", "8"),
    (r"how many.*color.*rainbow", "7"),
    (r"how many.*(?:state|states).*(?:united|us|usa|america)", "50"),
    (r"how many.*(?:player|players).*basketball.*court", "5"),
    (r"how many.*(?:player|players).*soccer.*field", "11"),
    (r"how many.*(?:player|players).*baseball.*field", "9"),
    (r"how many.*(?:square|squares).*chess", "64"),
    (r"how many.*(?:piece|pieces).*chess", "32"),
    (r"how many.*(?:string|strings).*guitar", "6"),
    (r"how many.*(?:string|strings).*violin", "4"),
    (r"how many.*(?:key|keys).*piano", "88"),
    (r"how many.*(?:bone|bones).*human", "206"),
    (r"how many.*(?:tooth|teeth).*adult", "32"),
    (r"how many.*(?:side|sides).*triangle", "3"),
    (r"how many.*(?:side|sides).*square", "4"),
    (r"how many.*(?:side|sides).*pentagon", "5"),
    (r"how many.*(?:side|sides).*hexagon", "6"),
    (r"how many.*(?:side|sides).*octagon", "8"),
    # -- "What religion" --
    (r"what.*religion.*(?:most.*popular|largest|most.*follow)", "christianity"),
    (r"what.*religion.*(?:second.*largest|second.*most)", "islam"),
    (r"what.*religion.*(?:oldest|third.*largest)", "hinduism"),
    (r"what.*religion.*(?:buddha|buddhist)", "buddhism"),
    (r"what.*religion.*(?:jew|jewish|torah)", "judaism"),
    (r"what.*religion.*(?:sikh|guru nanak)", "sikhism"),
    (r"what.*religion.*(?:jain)", "jainism"),
    (r"what.*religion.*(?:shinto)", "shinto"),
    # -- "What is the name of" catch-all --
    (r"what.*name.*first.*president.*usa|first.*president.*usa.*name", "george washington"),
    (r"what.*name.*current.*president.*usa|current.*president.*usa.*name", "joe biden"),
    (r"what.*name.*first.*prime minister.*india", "jawaharlal nehru"),
    (r"what.*name.*longest.*river|river.*longest.*name", "nile"),
    (r"what.*name.*largest.*(?:country|nation)", "russia"),
    (r"what.*name.*smallest.*(?:country|nation)", "vatican city"),
    # -- True or false catch-alls --
    (r"true or false.*earth.*flat", "false"),
    (r"true or false.*earth.*round", "true"),
    (r"true or false.*sun.*rotate.*earth|sun.*go.*around.*earth", "false"),
    (r"true or false.*earth.*rotate.*sun|earth.*go.*around.*sun", "true"),
    (r"true or false.*water.*wet", "true"),
    (r"true or false.*fire.*cold", "false"),
    (r"true or false.*human.*can.*fly", "false"),
    (r"true or false.*fish.*can.*swim", "true"),
    (r"true or false.*bird.*can.*fly.*(?!penguin|ostrich|kiwi|emu)", "true"),
    (r"true or false.*penguin.*fly", "false"),
    (r"true or false.*ostrich.*fly", "false"),
    (r"true or false.*spider.*insect", "false"),
    (r"true or false.*whale.*fish", "false"),
    (r"true or false.*whale.*mammal", "true"),
    (r"true or false.*dolphin.*fish", "false"),
    (r"true or false.*bat.*bird", "false"),
    (r"true or false.*tomato.*vegetable", "false"),
    (r"true or false.*tomato.*fruit", "true"),
    (r"true or false.*strawberry.*berry", "false"),
    (r"true or false.*banana.*berry", "true"),


]
CORE_COUNTS = [4, 8, 6, 12]
COLOR_DEPTHS = [24, 30]
LANGUAGES = [
    ("en-US", ["en-US", "en"]),
    ("en-GB", ["en-GB", "en"]),
]

_SITEKEY_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════
# TLS Session (curl_cffi — free Chrome fingerprint)
# ═══════════════════════════════════════════════════════════════

def make_session(proxy: Optional[str] = None) -> cffi_requests.Session:
    s = cffi_requests.Session()
    s.headers.update({
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache", "pragma": "no-cache",
        "sec-ch-ua": f'"Chromium";v="{CHROME_VERSION}", "Google Chrome";v="{CHROME_VERSION}", "Not?A_Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": CHROME_UA,
    })
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


# ═══════════════════════════════════════════════════════════════
# Synthetic Motion Data Generator
# ═══════════════════════════════════════════════════════════════

class MotionData:
    """Generates realistic fake motion data in hCaptcha's exact JSON format."""

    def __init__(self):
        self.base_ms = int(time.time() * 1000)
        self.screen_w, self.screen_h = random.choice(SCREEN_SIZES)
        self.color_depth = random.choice(COLOR_DEPTHS)
        self.cores = random.choice(CORE_COUNTS)
        self.lang, self.langs = random.choice(LANGUAGES)
        self.counter = 0
        self.params = {}
        mp = MODELS_DIR / "motion_params.json"
        if mp.exists():
            try:
                with open(mp) as f:
                    self.params = json.load(f)
            except Exception:
                pass

    def _tick(self, ms: int = 0) -> int:
        if ms:
            self.counter += ms
        else:
            mean_pause = self.params.get("mean_pause") or 16
            lo = max(1, int(mean_pause * 0.6))
            hi = max(lo + 1, int(mean_pause * 1.6))
            self.counter += random.randint(lo, hi)
        return self.base_ms + self.counter

    def _human_path(self, start: Tuple[int, int], end: Tuple[int, int],
                    points: int = 30) -> List[List[int]]:
        if self.params.get("mean_points"):
            points = max(8, min(60, int(self.params["mean_points"])))
        path = []
        sx, sy = start
        ex, ey = end
        for i in range(points):
            t = i / (points - 1)
            cx = sx + (ex - sx) * 0.4 + random.randint(-8, 8)
            cy = sy + (ey - sy) * 0.3 + random.randint(-6, 6)
            x = int((1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t ** 2 * ex)
            y = int((1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t ** 2 * ey)
            x += random.randint(-1, 1)
            y += random.randint(-1, 1)
            path.append([x, y, self._tick(6 + random.randint(0, 8))])
        return path

    def get_captcha_motion(self) -> dict:
        widget_x = random.randint(0, self.screen_w - 310)
        widget_y = random.randint(0, self.screen_h - 85)
        start = (random.randint(300, self.screen_w - 300),
                 random.randint(100, self.screen_h - 200))
        end_center = (widget_x + 30, widget_y + 37)
        path = self._human_path(start, end_center)
        mm = [[x - widget_x, y - widget_y, t] for x, y, t in path]
        periods = [(mm[i + 1][2] - mm[i][2]) for i in range(len(mm) - 1)]
        avg_period = sum(periods) / len(periods) if periods else 0
        return {
            "st": self.base_ms, "mm": mm, "mm-mp": avg_period,
            "md": [mm[-1][:2] + [self._tick(50)]], "md-mp": 0,
            "mu": [mm[-1][:2] + [self._tick(100)]], "mu-mp": 0,
            "v": 1,
            "topLevel": self._top_level(widget_x, widget_y, start),
            "session": [],
            "widgetList": ["0" + "".join(random.choices("abcdef0123456789", k=10))],
            "widgetId": "0" + "".join(random.choices("abcdef0123456789", k=10)),
            "href": f"https://{self.host}/",
            "prev": {"escaped": False, "passed": False,
                     "expiredChallenge": False, "expiredResponse": False},
        }

    def get_check_motion(self) -> dict:
        widget_x = random.randint(0, self.screen_w - 310)
        widget_y = random.randint(0, self.screen_h - 85)
        start = (random.randint(300, self.screen_w - 300),
                 random.randint(100, self.screen_h - 200))
        end_center = (widget_x + 30, widget_y + 37)
        path = self._human_path(start, end_center)
        mm = [[x - widget_x, y - widget_y, t] for x, y, t in path]
        periods = [(mm[i + 1][2] - mm[i][2]) for i in range(len(mm) - 1)]
        avg_period = sum(periods) / len(periods) if periods else 0
        return {
            "st": self.base_ms, "mm": mm, "mm-mp": avg_period,
            "md": [mm[-1][:2] + [self._tick(50)]], "md-mp": 0,
            "mu": [mm[-1][:2] + [self._tick(100)]], "mu-mp": 0,
            "v": 1,
            "topLevel": self._top_level(widget_x, widget_y, start),
            "session": [], "widgetList": [], "widgetId": "",
            "href": f"https://{self.host}/",
            "prev": {"escaped": False, "passed": False,
                     "expiredChallenge": False, "expiredResponse": False},
        }

    host = "discord.com"  # default; overwritten by HCaptchaSolver

    def _top_level(self, widget_x, widget_y, start) -> dict:
        taskbar = random.choice([0, 30, 40, 48])
        avail_h = max(1, self.screen_h - taskbar)
        start = (0, random.randint(100, self.screen_h - 200))
        end = (widget_x + random.randint(10, 280),
               widget_y + random.randint(10, 60))
        mm = self._human_path(start, end, 20)
        return {
            "inv": False,
            "st": self.base_ms - random.randint(200, 800),
            "sc": {
                "availWidth": self.screen_w, "availHeight": avail_h,
                "width": self.screen_w, "height": self.screen_h,
                "colorDepth": self.color_depth, "pixelDepth": self.color_depth,
                "top": 0, "left": 0, "availTop": 0, "availLeft": 0,
            },
            "nv": {
                "vendor": "Google Inc.", "vendorSub": "",
                "cookieEnabled": True, "webdriver": False,
                "hardwareConcurrency": self.cores,
                "userAgent": CHROME_UA, "language": self.lang,
                "languages": self.langs, "onLine": True,
                "doNotTrack": None, "maxTouchPoints": 0,
                "pdfViewerEnabled": True,
                "plugins": ["internal-pdf-viewer"] if random.random() > 0.3 else [],
            },
            "dr": "", "exec": False,
            "wn": [[self.screen_w, self.screen_h, 1, self.base_ms - 500]],
            "wn-mp": 0,
            "xy": [[0, 0, 1, self.base_ms - 500]], "xy-mp": 0,
            "mm": mm,
            "mm-mp": sum((mm[i+1][2]-mm[i][2]) for i in range(len(mm)-1)) / max(len(mm)-1, 1),
        }


# ═══════════════════════════════════════════════════════════════
# HSW Token Generator (Playwright)
# ═══════════════════════════════════════════════════════════════

class HSWGenerator:
    """Generates the HSW proof-of-work token hCaptcha requires."""

    def __init__(self, sitekey: str, host: str, version: str, proxy: Optional[str] = None):
        self.sitekey = sitekey
        self.host = host
        self.version = version
        self.proxy = proxy
        self._hsw_js: Optional[str] = None
        self._browser = None
        self._context = None

    async def _ensure_js(self, session: cffi_requests.Session, req_token: str):
        if self._hsw_js is not None:
            return
        try:
            import base64
            payload = req_token.split(".")[1]
            payload += "=" * (4 - len(payload) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload))
            hsw_url = f"https://newassets.hcaptcha.com{decoded['l']}/hsw.js"
        except Exception:
            hsw_url = f"https://newassets.hcaptcha.com/c/{self.version}/hsw.js"
        resp = session.get(hsw_url)
        self._hsw_js = resp.text

    async def _get_page(self):
        if self._browser is None:
            from browser_engine import async_playwright, ENGINE
            import stealth as _st
            pw = await async_playwright().start()
            launch_kw = {
                "headless": True,
                "args": _st.launch_args(headless=True) + [
                    "--disable-web-security",
                    "--window-size=1920,1080",
                ],
            }
            if self.proxy:
                launch_kw["proxy"] = {"server": self.proxy}
            self._browser = await pw.chromium.launch(**launch_kw)
            self._pw = pw
            _fp = {"cores": 8, "device_memory": 8, "touch_points": 0,
                   "locale": "en-US", "languages": ["en-US", "en"],
                   "locale_profile": None, "gpu": None, "pixel_ratio": 1.0}
            self._context = await self._browser.new_context(
                **_st.build_context_options(
                    _fp, CHROME_UA, viewport={"width": 1920, "height": 1080}
                )
            )
            await self._context.add_init_script(
                _st.build_init_script(_fp, CHROME_UA)
            )

    async def generate(self, session: cffi_requests.Session,
                       req_token: str) -> Optional[str]:
        await self._ensure_js(session, req_token)
        await self._get_page()
        page = await self._context.new_page()
        try:
            await page.route(
                "**/*",
                lambda route: route.fulfill(
                    status=200, content_type="text/html",
                    body="<html><head></head><body></body></html>",
                ),
            )
            await page.goto(f"https://{self.host}/", wait_until="domcontentloaded", timeout=10000)
            await page.evaluate(self._hsw_js)
            for _ in range(30):
                try:
                    if await page.evaluate("typeof hsw === 'function'"):
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.02)
            result = await page.evaluate("(req) => hsw(req)", req_token)
            return result
        finally:
            await page.close()

    async def close(self):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if hasattr(self, '_pw'):
            await self._pw.stop()


# ═══════════════════════════════════════════════════════════════
# DOM helpers
# ═══════════════════════════════════════════════════════════════

def _is_valid_sitekey(value: str) -> bool:
    v = (value or "").strip()
    return bool(_SITEKEY_RE.match(v))


async def extract_hcaptcha_sitekey(page) -> str:
    """Pull the hCaptcha sitekey from DOM, iframe src, or hcaptcha global."""
    # Strategy 1: [data-sitekey]
    try:
        sk = await page.evaluate("""() => {
            const el = document.querySelector('[data-sitekey]');
            return el ? el.getAttribute('data-sitekey') : '';
        }""")
        if _is_valid_sitekey(str(sk)):
            return str(sk).strip()
    except Exception:
        pass
    # Strategy 2: hcaptcha iframe src
    try:
        src = await page.evaluate("""() => {
            const f = document.querySelector('iframe[src*="hcaptcha.com"]');
            return f ? f.src : '';
        }""")
        m = re.search(r"sitekey=([^&]+)", src or "")
        if m and _is_valid_sitekey(m.group(1)):
            return m.group(1)
    except Exception:
        pass
    # Strategy 3: scan all iframes
    try:
        sitekey = await page.evaluate("""() => {
            const iframes = document.querySelectorAll('iframe');
            for (const f of iframes) {
                const m = (f.src || '').match(/sitekey=([^&#]+)/);
                if (m) return m[1];
            }
            return '';
        }""")
        if _is_valid_sitekey(sitekey):
            return sitekey.strip()
    except Exception:
        pass
    # Strategy 4: hcaptcha global
    try:
        sk = await page.evaluate("""() => {
            if (window.hcaptcha && window.hcaptcha.getSitekey) {
                try { return window.hcaptcha.getSitekey(); } catch(e) {}
            }
            return '';
        }""")
        if _is_valid_sitekey(str(sk)):
            return str(sk).strip()
    except Exception:
        pass
    return ""


async def read_hcaptcha_token(page) -> Optional[str]:
    """Read the current h-captcha-response token from the page."""
    try:
        token = await page.evaluate("""() => {
            const ta = document.querySelector('textarea[name="h-captcha-response"]');
            if (ta && ta.value && ta.value.length > 20) return ta.value;
            if (window.hcaptcha && window.hcaptcha.getResponse) {
                const r = window.hcaptcha.getResponse();
                if (r && r.length > 20) return r;
            }
            return '';
        }""")
        if token:
            return token
    except Exception:
        pass
    return None


async def set_hcaptcha_token_on_page(page, token: str) -> bool:
    """Inject a solved token into the hCaptcha textarea."""
    try:
        result = await page.evaluate(f""""() => {{
            const ta = document.querySelector('textarea[name="h-captcha-response"]');
            if (ta) {{
                ta.value = '{token}';
                ta.dispatchEvent(new Event('input', {{bubbles: true}}));
                ta.dispatchEvent(new Event('change', {{bubbles: true}}));
                return true;
            }}
            return false;
        }}""")
        return bool(result)
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
# Offline tile similarity (FunCAPTCHA / Arkose)
# ═══════════════════════════════════════════════════════════════

def _tile_signature(img: Image.Image) -> List[float]:
    small = img.resize((32, 32), Image.LANCZOS)
    gray = small.convert('L')
    avg_brightness = sum(gray.getdata()) / (32 * 32)
    pixels = list(small.getdata())
    n = len(pixels)
    r_avg = sum(p[0] for p in pixels) / n
    g_avg = sum(p[1] for p in pixels) / n
    b_avg = sum(p[2] for p in pixels) / n
    variance = sum((p[0] - r_avg)**2 + (p[1] - g_avg)**2 + (p[2] - b_avg)**2
                   for p in pixels) / n
    edge_sum = 0
    for y in range(32):
        for x in range(31):
            edge_sum += abs(gray.getpixel((x + 1, y)) - gray.getpixel((x, y)))
    edge_density = edge_sum / (32 * 31)
    return [avg_brightness / 255, r_avg / 255, g_avg / 255, b_avg / 255,
            variance / 50000, edge_density / 50]


def _signature_distance(sig1: List[float], sig2: List[float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(sig1, sig2)))


def find_matching_tiles_by_similarity(tiles: List[Image.Image],
                                      threshold: float = 0.15) -> List[int]:
    if len(tiles) < 3:
        return list(range(len(tiles)))
    sigs = [_tile_signature(t) for t in tiles]
    n_dims = len(sigs[0])
    median_sig = []
    for dim in range(n_dims):
        vals = sorted(s[dim] for s in sigs)
        median_sig.append(vals[len(vals) // 2])
    distances = [_signature_distance(s, median_sig) for s in sigs]
    avg_dist = sum(distances) / len(distances)
    adaptive = max(threshold, avg_dist * 1.2)
    matching = [i for i, d in enumerate(distances) if d > adaptive]
    if len(matching) > len(tiles) * 0.7:
        return []
    return matching


def split_grid_screenshot(screenshot_bytes: bytes,
                          grid_size: int = 3) -> List[Image.Image]:
    img = Image.open(io.BytesIO(screenshot_bytes))
    w, h = img.size
    margin_x = int(w * 0.02)
    margin_y = int(h * 0.02)
    tile_w = (w - 2 * margin_x) // grid_size
    tile_h = (h - 2 * margin_y) // grid_size
    if tile_w < 20 or tile_h < 20:
        return []
    tiles = []
    for row in range(grid_size):
        for col in range(grid_size):
            left = margin_x + col * tile_w
            top = margin_y + row * tile_h
            tile = img.crop((left, top, left + tile_w, top + tile_h))
            tiles.append(tile.resize((128, 128), Image.LANCZOS))
    return tiles


# ── FunCAPTCHA solver ───────────────────────────────────────

FUNCAPTCHA_SELECTORS = [
    'iframe[src*="funcaptcha"]', 'iframe[src*="arkose"]',
    'iframe[title*="captcha"]', 'iframe[src*="captcha"]',
    '[id*="funcaptcha"]', '[class*="funcaptcha"]',
    '[class*="Challenge"]',
]


async def extract_funcaptcha_task(page, iframe=None) -> str:
    try:
        if iframe:
            text = await iframe.evaluate("""() => {
                const els = document.querySelectorAll('[class*="challenge"], [class*="prompt"], [class*="instruction"], [class*="header"], h1, h2, [class*="title"]');
                for (const el of els) {
                    const t = (el.textContent || '').trim();
                    if (t.length > 6 && t.length < 200) return t;
                }
                return document.body ? document.body.innerText.slice(0, 300) : '';
            }""")
            if text and len(str(text).strip()) > 5:
                return str(text).strip()
    except Exception:
        pass
    try:
        text = await page.evaluate("""() => {
            const el = document.querySelector('[class*="challenge"], [class*="prompt"], [class*="instruction"], [class*="header"]');
            return el ? el.textContent.trim().slice(0, 200) : '';
        }""")
        if text and len(str(text).strip()) > 5:
            return str(text).strip()
    except Exception:
        pass
    return ""


async def solve_funcaptcha_pixels(page, iframe=None,
                                  log: Optional[Callable] = None) -> bool:
    """Solve a FunCAPTCHA/Arkose tile challenge offline via pixel similarity."""
    log = log or (lambda msg, level="info": None)
    if iframe is None:
        for sel in FUNCAPTCHA_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    iframe = el
                    break
            except Exception:
                pass
            await asyncio.sleep(0.3)
    if not iframe:
        log("[FunCAPTCHA] No challenge element found", level="warn")
        return False
    await asyncio.sleep(1)

    clicked = 0

    # Try DOM tile boxes
    tile_boxes = []
    try:
        data = await iframe.evaluate("""() => {
            const selectors = '.task-image, [class*="image"], [role="button"] > div, ' +
                              '.grid-item, .cell, td, img[class*="task"], ' +
                              '.image-grid > div, [class*="tile"]';
            const els = document.querySelectorAll(selectors);
            const out = [];
            els.forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width > 30 && r.height > 30 && r.width < 500 && r.height < 500) {
                    out.push({x: r.x, y: r.y, w: r.width, h: r.height});
                }
            });
            return JSON.stringify(out);
        }""")
        if data:
            boxes = json.loads(data)
            if len(boxes) >= 2:
                ibox = await iframe.bounding_box()
                if ibox:
                    tile_boxes = [{'x': ibox['x'] + b['x'], 'y': ibox['y'] + b['y'],
                                   'w': b['w'], 'h': b['h']} for b in boxes]
    except Exception:
        pass

    if len(tile_boxes) >= 2:
        tiles = []
        valid = []
        for i, b in enumerate(tile_boxes):
            try:
                clip = {'x': b['x'], 'y': b['y'], 'width': b['w'], 'height': b['h']}
                shot = await page.screenshot(clip=clip)
                tiles.append(Image.open(io.BytesIO(shot)).resize((128, 128), Image.LANCZOS))
                valid.append(i)
            except Exception:
                tiles.append(None)
        if len(valid) >= 2:
            sig_tiles = [tiles[i] for i in valid]
            local = find_matching_tiles_by_similarity(sig_tiles)
            matching = [valid[i] for i in local] if local else []
            if not matching:
                matching = valid
            for idx in matching:
                b = tile_boxes[idx]
                try:
                    await page.mouse.click(b['x'] + b['w'] / 2, b['y'] + b['h'] / 2)
                    clicked += 1
                except Exception:
                    pass
                await asyncio.sleep(0.2)
            log(f"[FunCAPTCHA] Clicked {clicked} tiles (DOM boxes)")

    # Grid split fallback
    if clicked == 0:
        try:
            box = await iframe.bounding_box()
            if box and box['width'] >= 100 and box['height'] >= 100:
                clip = {'x': box['x'], 'y': box['y'],
                        'width': box['width'], 'height': box['height']}
                shot = await page.screenshot(clip=clip)
                img = Image.open(io.BytesIO(shot))
                w, h = img.size
                grid = 4 if (w / h) > 1.5 else 3
                tiles = split_grid_screenshot(shot, grid)
                if len(tiles) >= 2:
                    matching = find_matching_tiles_by_similarity(tiles) or list(range(len(tiles)))
                    margin_x = int(box['width'] * 0.02)
                    margin_y = int(box['height'] * 0.02)
                    tile_w = (box['width'] - 2 * margin_x) / grid
                    tile_h = (box['height'] - 2 * margin_y) / grid
                    for idx in matching:
                        row, col = divmod(idx, grid)
                        x = box['x'] + margin_x + col * tile_w + tile_w / 2
                        y = box['y'] + margin_y + row * tile_h + tile_h / 2
                        try:
                            await page.mouse.click(x, y)
                            clicked += 1
                        except Exception:
                            pass
                        await asyncio.sleep(0.25)
                    log(f"[FunCAPTCHA] Clicked {clicked} tiles (grid split)")
        except Exception as e:
            log(f"[FunCAPTCHA] grid split error: {e}", level="warn")

    if clicked == 0:
        return False

    # Submit button
    try:
        await iframe.evaluate("""() => {
            const btns = document.querySelectorAll('button, [role="button"], [type="submit"]');
            for (const b of btns) {
                const t = (b.textContent || '').toLowerCase();
                if (b.offsetParent !== null &&
                    (t.includes('verify') || t.includes('submit') ||
                     t.includes('continue') || t.includes('done'))) {
                    b.click();
                    return;
                }
            }
        }""")
    except Exception:
        pass
    await asyncio.sleep(2.5)

    # Check solved
    try:
        solved = await page.evaluate("""() => {
            const fc = document.querySelector('textarea[name="fc-token"]');
            if (fc && fc.value && fc.value.length > 10) return 'fc-token';
            const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
            if (ta && ta.value && ta.value.length > 10) return 'recaptcha';
            const ch = document.querySelector('[class*="challenge" i], [class*="Challenge"]');
            if (ch && getComputedStyle(ch).display === 'none') return 'hidden';
            return '';
        }""")
    except Exception:
        solved = ""
    if solved:
        log(f"[FunCAPTCHA] SOLVED ({solved})")
        return True
    return False


# ═══════════════════════════════════════════════════════════════
# Brain-Based hCaptcha Solver (curl_cffi API flow)
# ═══════════════════════════════════════════════════════════════

class HCaptchaSolver:
    """Universal hCaptcha solver using trained brains + direct API calls."""

    def __init__(self, sitekey: str, host: str, proxy: Optional[str] = None,
                 model_path: Optional[str] = None):
        self.sitekey = sitekey
        self.host = host.split("//")[-1].split("/")[0]
        self.proxy = proxy
        self.session = make_session(proxy)
        self.motion = MotionData()
        self.motion.host = self.host

        resp = self.session.get("https://hcaptcha.com/1/api.js",
                                params={"render": "explicit"})
        versions = re.findall(r"v1/([A-Za-z0-9]+)/static", resp.text)
        self.version = versions[1] if len(versions) > 1 else DEFAULT_VERSION
        print(f"  hCaptcha v{self.version[:8]}...")

    def get_config(self) -> Optional[dict]:
        params = {
            "v": self.version, "sitekey": self.sitekey,
            "host": self.host, "sc": "1", "swa": "1", "spst": "1",
        }
        resp = self.session.post(f"{HCAPTCHA_API}/checksiteconfig", params=params)
        if resp.status_code != 200:
            return None
        return resp.json()

    async def fetch_challenge(self, config: dict,
                               hsw: HSWGenerator) -> Optional[dict]:
        req = config["c"]["req"]
        token = await hsw.generate(self.session, req)
        if not token:
            return None
        data = {
            "v": self.version, "sitekey": self.sitekey,
            "host": self.host, "hl": "en-US",
            "motionData": json.dumps(self.motion.get_captcha_motion()),
            "n": token, "c": json.dumps(config["c"]),
        }
        resp = self.session.post(
            f"{HCAPTCHA_API}/getcaptcha/{self.sitekey}", data=data)
        if resp.status_code != 200:
            return None
        return resp.json()

    async def submit(self, challenge: dict, answers: dict,
                      hsw: HSWGenerator) -> Optional[dict]:
        req = challenge["c"]["req"]
        token = await hsw.generate(self.session, req)
        if not token:
            return None
        endpoint = f"{HCAPTCHA_API}/checkcaptcha/{self.sitekey}/{challenge['key']}"
        payload = json.dumps({
            "v": self.version, "sitekey": self.sitekey,
            "serverdomain": self.host,
            "job_mode": challenge["request_type"],
            "motionData": json.dumps(self.motion.get_check_motion()),
            "n": token, "c": json.dumps(challenge["c"]),
            "answers": answers,
        })
        headers = {
            "content-type": "application/json;charset=UTF-8",
            "accept": "*/*",
            "origin": "https://newassets.hcaptcha.com",
            "referer": "https://newassets.hcaptcha.com/",
        }
        resp = self.session.post(endpoint, data=payload, headers=headers)
        if resp.status_code == 200:
            result = resp.json()
            if result.get("pass"):
                return {"success": True, "token": result.get("generated_pass_UUID")}
            if result.get("success") is False:
                return {"success": False, "error": result.get("error-codes", [])}
        return {"success": False, "error": f"HTTP {resp.status_code}"}

    async def solve(self, max_attempts: int = 10) -> dict:
        config = self.get_config()
        if not config:
            return {"success": False, "error": "Config failed"}
        if "c" not in config:
            return {"success": False, "error": "No config['c']"}

        hsw = HSWGenerator(self.sitekey, self.host, self.version, self.proxy)
        start = time.time()

        try:
            for attempt in range(1, max_attempts + 1):
                print(f"\n── Attempt {attempt}/{max_attempts} ──")

                challenge = await self.fetch_challenge(config, hsw)
                if not challenge:
                    config = self.get_config()
                    if not config:
                        continue
                    challenge = await self.fetch_challenge(config, hsw)
                    if not challenge:
                        continue

                if challenge.get("generated_pass_UUID"):
                    elapsed = time.time() - start
                    print(f"  ✅ Passive pass! ({elapsed:.1f}s)")
                    await hsw.close()
                    return {"success": True, "token": challenge["generated_pass_UUID"],
                            "time": elapsed}

                req_type = challenge.get("request_type", "unknown")
                print(f"  Type: {req_type}")

                if req_type == "image_label_area_select":
                    answers = {}
                    for task in challenge.get("tasklist", []):
                        answers[task["task_key"]] = [{
                            "entity_name": 0,
                            "entity_type": "default",
                            "entity_coords": [200, 150],
                        }]
                else:
                    print(f"  ⚠️  Unsupported: {req_type}")
                    continue

                result = await self.submit(challenge, answers, hsw)
                if result and result.get("success"):
                    elapsed = time.time() - start
                    print(f"  ✅ Solved! ({elapsed:.1f}s)")
                    await hsw.close()
                    return {"success": True, "token": result.get("token", ""),
                            "time": elapsed}
                error = result.get("error", "unknown") if result else "none"
                print(f"  ❌ Rejected: {error}")
                config = self.get_config()

            await hsw.close()
            return {"success": False, "error": f"Max {max_attempts} attempts",
                    "time": time.time() - start}
        except Exception as e:
            await hsw.close()
            return {"success": False, "error": str(e),
                    "time": time.time() - start}


# ═══════════════════════════════════════════════════════════════
# hCaptcha Accessibility Challenge Solver (Ollama vision)
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
# Animal word list — every known animal name (lowercase)
# Used for hCaptcha "pick the animal" accessibility challenges
# ═══════════════════════════════════════════════════════════════
ANIMAL_WORDS = frozenset([
    # Baby animals (frequently used in pick-the-animal challenges)
    "piglet","kitten","puppy","calf","chick","lamb","foal","fawn","duckling",
    "gosling","kid","cub","joey","cygnet","eaglet","owlet","leveret","pullet",
    "heifer","hatchling","fry","bunny","kit","filly","colt","sprat","parr",
    "aardvark","abalone","agouti","albatross","alligator","alpaca","anaconda",
    "angelfish","angelshark","ant","anteater","antelope","ape","aphid",
    "armadillo","asp","axolotl","baboon","badger","bandicoot","barnacle",
    "barracuda","basilisk","bass","bat","bear","beaver","bee","beetle",
    "bilby","binturong","bison","blackbird","blowfish","bluebird","boa",
    "bobcat","bongo","bonobo","buffalo","bull","bullfrog","bumblebee",
    "butterfly","caiman","camel","canary","capybara","caracal","cardinal",
    "caribou","carp","caterpillar","cat","catfish","cattle","centipede",
    "chameleon","cheetah","chickadee","chicken","chimpanzee","chinchilla",
    "chipmunk","cicada","clam","clownfish","coati","cobra","cockatoo",
    "cockroach","cod","collie","conch","condor","coral","cougar","cow",
    "coyote","coypu","crab","crane","crayfish","cricket","crocodile",
    "crow","cuckoo","cuttlefish","deer","dingo","dodo","dog","dolphin",
    "donkey","dove","dragon","dragonfly","dromedary","duck","dugong",
    "eagle","earthworm","earwig","echidna","eel","egret","eland",
    "elephant","elk","emu","ermine","falcon","ferret","finch","firefly",
    "flamingo","flea","flounder","fly","fossa","fox","frog","gar",
    "gazelle","gecko","gerbil","gibbon","giraffe","gnat","gnu","goat",
    "goldfinch","goldfish","goose","gopher","gorilla","grasshopper",
    "grouper","grouse","gull","guppy","haddock","halibut","hamster",
    "hare","hawk","hedgehog","hen","heron","herring","hippopotamus","hornet",
    "horse","hummingbird","husky","hyena","hyrax","ibis","iguana",
    "impala","indri","jackal","jackrabbit","jaguar","jay","jellyfish",
    "jerboa","kangaroo","katydid","kinkajou","kiwi","koala","kookaburra",
    "krill","kudu","ladybug","lamprey","lark","leech","lemming","lemur",
    "leopard","lion","lizard","llama","lobster","locust","loon","loris",
    "louse","lynx","macaque","macaw","mackerel","maggot","mallard",
    "mamba","manatee","mandrill","mantis","marmot","marten","meerkat",
    "mink","minnow","mockingbird","mole","mongoose","monkey","moose",
    "mosquito","moth","mouse","mule","muskrat","mussel","narwhal",
    "nautilus","newt","nightingale","numbat","nuthatch","nutria",
    "ocelot","octopus","okapi","opossum","orangutan","orca","oriole",
    "ostrich","otter","owl","ox","oyster","panda","pangolin","panther",
    "parakeet","parrot","peacock","peafowl","pelican","penguin",
    "pheasant","phoenix","pig","pigeon","pika","piranha","platypus",
    "pony","porcupine","porpoise","pronghorn","puffin","pug","puma",
    "python","quail","quetzal","quokka","quoll","rabbit","raccoon",
    "rat","rattlesnake","raven","reindeer","rhinoceros","robin",
    "rooster","salamander","salmon","sandpiper","sardine","sawfish",
    "scallop","scorpion","seahorse","seal","serval","shark","sheep",
    "shrew","shrimp","silkworm","silverfish","skink","skunk","sloth",
    "slug","snail","snake","sparrow","spider","sponge","squid",
    "squirrel","starfish","starling","stingray","stoat","stork",
    "sturgeon","swan","swordfish","tadpole","tamarin","tapir",
    "tarantula","tarpon","tarsier","termite","tern","thrush","tiger",
    "toad","tortoise","toucan","trout","tuatara","tuna","turkey",
    "turtle","unicorn","vaquita","vicuna","viper","vole","vulture",
    "wallaby","walrus","warthog","wasp","weasel","whale","wildebeest",
    "wolf","wolverine","wombat","woodchuck","woodpecker","worm","wren",
    "yak","zebra","zebu","zorse",
    # Dinosaurs
    "allosaurus","ankylosaurus","apatosaurus","brachiosaurus",
    "brontosaurus","diplodocus","iguanodon","megalodon","plesiosaur",
    "pterodactyl","pterosaur","stegosaurus","triceratops",
    "tyrannosaurus","velociraptor",
    # Mythical/extinct (some captchas include these)
    "dragon","griffin","phoenix","pegasus","centaur","hydra",
    "kraken","leviathan","manticore","minotaur","wyvern",
    "werewolf","yeti","bigfoot","sasquatch","nessie","chupacabra",
    # Sea creatures / marine
    "anemone","coral","jellyfish","manowar","nautilus","urchin",
    "barnacle","limpet","abalone","conch","whelk","cuttlefish",
    # Birds - additional
    "albatross","booby","budgerigar","budgie","bustard","cassowary",
    "cockatiel","cormorant","curlew","dodo","dunlin","falcon",
    "flamingo","frigatebird","gannet","godwit","guineafowl",
    "hoopoe","hornbill","jacana","kestrel","kingfisher","kiwi",
    "lapwing","magpie","martin","merlin","moorhen","myna","oriole",
    "osprey","owl","oystercatcher","partridge","pelican","penguin",
    "petrel","plover","puffin","quail","rail","razorbill",
    "roadrunner","rook","ruff","sanderling","shearwater","shrike",
    "skua","skylark","snipe","spoonbill","stilt","stint","swallow",
    "swift","tanager","titmouse","towhee","turnstone","vireo",
    "vulture","wagtail","warbler","waxwing","weaver","whimbrel",
    "whipbird","willet","yellowhammer",
    # Fish - additional
    "anchovy","anglerfish","arowana","barracuda","blenny","bream",
    "burbot","butterflyfish","carp","catla","char","chub","cichlid",
    "coelacanth","damselfish","darter","dory","dragonet","eel",
    "filefish","flatfish","flounder","goby","grouper","grunion",
    "gudgeon","guitarfish","gunnel","gurnard","hagfish","hake",
    "halfbeak","hamlet","hogfish","icefish","jawfish","killifish",
    "lamprey","ling","lionfish","loach","mackerel","marlin",
    "mooneye","mudskipper","mullet","needlefish","opah","parrotfish",
    "perch","pickerel","pike","pilchard","pipefish","plaice",
    "pompano","pufferfish","pupfish","rattail","remora","roach",
    "rockfish","rudderfish","sailfish","salmon","scorpionfish",
    "sculpin","shad","skate","smelt","snapper","snook","sole",
    "sprat","stickleback","stingray","stonefish","sturgeon",
    "sunfish","surgeonfish","swordfish","tang","tarpon","tench",
    "tetra","tilapia","triggerfish","trout","tuna","turbot",
    "wahoo","walleye","weakfish","whitefish","whiting","wolffish",
    "wrasse","yellowtail","zander",
    # Amphibians & Reptiles - additional
    "adder","agamid","alligator","anole","axolotl","bullfrog",
    "caiman","caecilian","chameleon","cobra","copperhead",
    "cottonmouth","dab","frog","gecko","gharial","gila",
    "iguana","krait","leopardfrog","mamba","monitor","mudpuppy",
    "newt","racer","racerunner","rattler","salamander","skink",
    "snake","springpeeper","taipan","terrapin","toad","tortoise",
    "treefrog","tuatara","turtle","viper","whiptail",
    # Insects / Bugs - additional
    "antlion","aphid","backswimmer","bedbug","bee","beetle",
    "borer","bristletail","bug","bumblebee","caddisfly","chafer",
    "chigger","cicada","cockroach","crane","cricket","damselfly",
    "dobsonfly","dragonfly","earwig","fireant","firefly","flea",
    "fly","fruitfly","gnat","grasshopper","grub","hornet",
    "horsefly","hoverfly","katydid","lacewing","ladybug",
    "lanternfly","leafcutter","leafhopper","lice","locust",
    "longhorn","louse","mantis","mayfly","midge","mite",
    "mosquito","moth","planthopper","potatobeetle","psyllid",
    "roach","robberfly","sawfly","scarab","silkworm","silverfish",
    "springtail","stinkbug","stonefly","termite","thrips","tick",
    "tsetse","walkingstick","wasp","weevil","whitefly","yellowjacket",
    # Arachnids
    "harvestman","mite","scorpion","spider","tarantula","tick",
    "vinegaroon","whipscorpion","whipspider",
    # Mollusks
    "abalone","arkclam","clam","conch","cowrie","cuttlefish",
    "geoduck","limpet","mussel","nautilus","octopus","oyster",
    "periwinkle","quahog","razorclam","scallop","slug","snail",
    "squid","triton","whelk",
    # Crustaceans
    "amphipod","barnacle","copepod","crab","crayfish","isopod",
    "krill","langoustine","lobster","prawn","sandhopper",
    "shrimp","sowbug","woodlouse",
    # Mammals - additional (bats, rodents, primates etc)
    "agouti","alpaca","anteater","armadillo","ayeaye","baboon",
    "badger","bandicoot","bat","bear","beaver","bilby","binturong",
    "bison","bobcat","bonobo","buffalo","bushbaby","camel",
    "capybara","caracal","caribou","cheetah","chimp","chinchilla",
    "chipmunk","coati","colobus","colugo","cougar","cow",
    "coyote","coypu","deer","dhole","dingo","dog","dolphin",
    "donkey","dormouse","dugong","echidna","eland","elephant",
    "elk","ermine","fennec","ferret","fisher","fossa","fox",
    "galago","gazelle","genet","gerbil","gibbon","giraffe",
    "goat","gopher","gorilla","grysbok","guanaco","hamster",
    "hare","hedgehog","hippo","hippopotamus","horse","human",
    "hutia","hyena","hyrax","ibex","impala","indri","jackal",
    "jaguar","jerboa","kangaroo","kinkajou","koala","kudu",
    "lemming","lemur","leopard","lion","llama","loris","lynx",
    "macaque","mammoth","manatee","mandrill","margay","marmoset",
    "marmot","marten","mastodon","meerkat","mink","mole",
    "mongoose","monkey","moose","mouse","mule","muntjac","muskox",
    "muskrat","narwhal","numbat","nutria","nyala","ocelot",
    "okapi","opossum","orangutan","orca","oryx","otter","panda",
    "pangolin","panther","peccary","pika","platypus","polecat",
    "pony","porcupine","possum","potoroo","pronghorn","pudu",
    "puma","quokka","quoll","rabbit","raccoon","rat","reindeer",
    "rhino","rhinoceros","sable","saiga","seal","serval",
    "sheep","shrew","siamang","skunk","sloth","solenodon",
    "springbok","springhare","squirrel","stoat","sugarglider",
    "sunbear","tamarin","tapir","tarsier","tiger","topi",
    "uakari","vicuna","vole","wallaby","walrus","warthog",
    "waterbuck","weasel","whale","wildebeest","wolf","wolverine",
    "wombat","woodchuck","yak","zebra","zebu","zorilla","zorro",
    # Dog breeds (sometimes captchas use these)
    "beagle","boxer","bulldog","chihuahua","collie","dalmatian",
    "doberman","greyhound","hound","husky","labrador","mastiff",
    "poodle","pug","retriever","rottweiler","shepherd","spaniel",
    "terrier","whippet",
    # Cat breeds
    "bengal","birman","burmese","calico","persian","siamese",
    "sphynx","tabby",
    # Horse breeds / equines
    "appaloosa","bronco","clydesdale","colt","filly","gelding",
    "mare","mustang","palomino","pony","stallion",
    # Collective / generic
    "amphibian","animal","arachnid","beast","bird","bovine",
    "bug","canine","cetacean","crustacean","dinosaur","equine",
    "feline","finch","fish","fowl","insect","invertebrate",
    "mammal","marsupial","mollusk","primate","raptor","reptile",
    "rodent","serpent","ungulate","vertebrate",
])

# --- Country knowledge: "choose the country" accessibility challenges ---
# hCaptcha shows 3 words (one is a country) and asks to pick the country.
COUNTRY_WORDS = frozenset([
    "afghanistan","albania","algeria","andorra","angola","antigua",
    "argentina","armenia","australia","austria","azerbaijan",
    "bahamas","bahrain","bangladesh","barbados","belarus","belgium",
    "belize","benin","bhutan","bolivia","botswana","brazil","brunei",
    "bulgaria","burundi",
    "cambodia","cameroon","canada","chad","chile","china","colombia",
    "comoros","congo","croatia","cuba","cyprus","czechia",
    "denmark","djibouti","dominica",
    "ecuador","egypt","eritrea","estonia","eswatini","ethiopia",
    "fiji","finland","france",
    "gabon","gambia","georgia","germany","ghana","greece","grenada",
    "guatemala","guinea","guyana",
    "haiti","honduras","hungary",
    "iceland","india","indonesia","iran","iraq","ireland","israel","italy",
    "jamaica","japan","jordan",
    "kazakhstan","kenya","kiribati","kuwait","kyrgyzstan",
    "laos","latvia","lebanon","lesotho","liberia","libya","liechtenstein",
    "lithuania","luxembourg",
    "madagascar","malawi","malaysia","maldives","mali","malta",
    "mauritania","mauritius","mexico","micronesia","moldova","monaco",
    "mongolia","montenegro","morocco","mozambique","myanmar",
    "namibia","nauru","nepal","netherlands","nicaragua","niger","nigeria",
    "norway",
    "oman",
    "pakistan","palau","palestine","panama","paraguay","peru",
    "philippines","poland","portugal",
    "qatar",
    "romania","russia","rwanda",
    "samoa","senegal","serbia","seychelles","singapore","slovakia",
    "slovenia","somalia","spain","sudan","suriname","sweden",
    "switzerland","syria",
    "taiwan","tajikistan","tanzania","thailand","togo","tonga","tunisia",
    "turkey","turkmenistan","tuvalu",
    "uganda","ukraine","uruguay","uzbekistan",
    "vanuatu","venezuela","vietnam",
    "yemen",
    "zambia","zimbabwe",
    # Common short / alternate names captchas sometimes use
    "america","britain","burma","czech","england","holland","korea",
    "scotland","swaziland","usa","wales",
])

# Multi-word country names (matched as full phrases).
COUNTRY_PHRASES = frozenset([
    "united states", "united states of america", "united kingdom",
    "south africa", "south korea", "north korea", "south sudan",
    "new zealand", "sri lanka", "costa rica", "saudi arabia",
    "united arab emirates", "papua new guinea", "san marino",
    "saint lucia", "saint kitts", "saint vincent", "east timor",
    "ivory coast", "trinidad and tobago", "dominican republic",
    "central african republic", "czech republic", "vatican city",
    "cape verde", "el salvador", "equatorial guinea",
    "bosnia and herzegovina", "marshall islands", "solomon islands",
    "sierra leone", "burkina faso", "guinea bissau",
])
# ── Knowledge-base solver for hCaptcha accessibility NL questions ──
# Covers: rooms, colors, animal sounds, counting/legs, calendar, nature,
# object function, opposites, and "which of these is a/an X" pickers.

KNOWLEDGE_QUESTIONS = [
    # ── Rooms ──
    (r"room.*(?:has|with) a sink|sink for washing dishes|wash.*dishes", "kitchen"),
    (r"room.*cook|room.*prepar(e|ing) food", "kitchen"),
    (r"room.*refrigerator|room.*fridge", "kitchen"),
    (r"room.*(?:has|with) a bed|room.*sleep", "bedroom"),
    (r"room.*(?:shower|bath|bathtub|bath tub)|room.*brush.*teeth", "bathroom"),
    (r"room.*(?:sofa|couch|watch tv|watch television)", "living room"),
    (r"room.*(?:eat dinner|dining table|dining)", "dining room"),
    (r"room.*(?:laundry|washing machine)", "laundry room"),
    (r"room.*(?:read|books)", "library"),
    (r"room.*(?:work|desk|office)", "office"),
    # ── Colors ──
    (r"color.*sky|color.*ocean|color.*sea|color.*water", "blue"),
    (r"color.*grass|color.*(?:leaf|leaves)", "green"),
    (r"color.*snow|color.*cloud|color.*milk", "white"),
    (r"color.*banana|color.*sun|color.*lemon", "yellow"),
    (r"color.*blood|color.*strawberr|color.*stop sign", "red"),
    (r"color.*orange|color.*carrot|color.*pumpkin", "orange"),
    (r"color.*chocolate|color.*(?:tree|trunk)|color.*brown", "brown"),
    (r"color.*coal|color.*night sky|color.*crow", "black"),
    (r"color.*elephant", "gray"),
    (r"color.*apple", "red"),
    (r"color.*grape|color.*eggplant|color.*plum", "purple"),
    (r"color.*pink|color.*flamingo|color.*pig", "pink"),
    (r"what color.*sky", "blue"),
    (r"what color.*grass", "green"),
    (r"what color.*snow", "white"),
    (r"what color.*banana|what color.*sun", "yellow"),
    (r"what color.*blood|what color.*stop sign", "red"),
    # ── Animal sounds → animal ──
    (r"animal.*moo|says moo|makes.*moo", "cow"),
    (r"animal.*(?:barks|bark)|says woof|makes.*woof", "dog"),
    (r"animal.*(?:meows|meow)|says meow|makes.*meow", "cat"),
    (r"animal.*(?:quacks|quack)|says quack|makes.*quack", "duck"),
    (r"animal.*(?:oinks|oink)|says oink|makes.*oink", "pig"),
    (r"animal.*(?:neighs|neigh)|says neigh|makes.*neigh", "horse"),
    (r"animal.*(?:baas|baa)|says baa|makes.*baa", "sheep"),
    (r"animal.*(?:roars|roar)|says roar|makes.*roar", "lion"),
    (r"animal.*(?:howls|howl)|says howl|makes.*howl", "wolf"),
    (r"animal.*(?:chirps|chirp|tweets|tweet|sings)", "bird"),
    (r"animal.*(?:ribbits|ribbit|croaks|croak)", "frog"),
    (r"animal.*(?:hisses|hiss)", "snake"),
    (r"animal.*(?:gobbles|gobble)", "turkey"),
    (r"animal.*(?:hoots|hoot)", "owl"),
    (r"animal.*(?:buzzes|buzz)", "bee"),
    (r"animal.*(?:clucks|cluck)", "chicken"),
    (r"animal.*(?:caws|caw)", "crow"),
    (r"animal.*(?:growls|growl)", "bear"),
    # ── Counting / legs / wheels ──
    (r"how many legs.*(?:dog|cat|horse|cow|goat|sheep|pig|rabbit)", "4"),
    (r"how many legs.*spider", "8"),
    (r"how many legs.*(?:insect|ant|bee|beetle|fly|bug|grasshopper)", "6"),
    (r"how many legs.*(?:bird|chicken|duck|person|human|man|woman)", "2"),
    (r"how many legs.*(?:snake|worm)", "0"),
    (r"how many wheels.*car", "4"),
    (r"how many wheels.*(?:bicycle|bike)", "2"),
    (r"how many wheels.*tricycle", "3"),
    (r"how many wheels.*(?:motorcycle|motorbike)", "2"),
    (r"how many wheels.*bus", "4"),
    (r"how many days.*week", "7"),
    (r"how many months(?!.*\b31\b).*year", "12"),
    (r"how many seasons", "4"),
    (r"how many eyes", "2"),
    (r"how many fingers.*(?:one|single)? ?hand", "5"),
    (r"how many toes.*(?:one|single)? ?foot", "5"),
    (r"how many colors.*rainbow|colors in a rainbow", "7"),
    (r"how many sides.*triangle", "3"),
    (r"how many sides.*square", "4"),
    (r"how many sides.*(?:pentagon|star)", "5"),
    (r"how many sides.*hexagon", "6"),
    (r"how many hours.*(?:day|in a day)", "24"),
    (r"how many (?:minutes|mins).*hour", "60"),
    (r"how many (?:letters|alphabet).*alphabet", "26"),
    (r"how many (?:planets)", "8"),
    (r"how many (?:wings).*bird", "2"),
    (r"how many ears", "2"),
    (r"how many nose", "1"),
    (r"how many heads", "1"),
    (r"how many teeth", "32"),
    # ── Calendar ──
    (r"first month.*year|month.*first.*year", "january"),
    (r"last month.*year|month.*last.*year", "december"),
    (r"month.*after june", "july"),
    (r"month.*after july", "august"),
    (r"month (?:that|with).*28 (?:or 29 )?days|month.*february", "february"),
    (r"season.*after winter", "spring"),
    (r"season.*after spring", "summer"),
    (r"season.*after summer", "autumn"),
    (r"season.*after (?:autumn|fall)", "winter"),
    (r"first day of the week", "sunday"),
    (r"day.*after tuesday", "wednesday"),
    (r"day.*after monday", "tuesday"),
    (r"day.*after sunday", "monday"),
    (r"day.*before friday", "thursday"),
    (r"day.*before monday", "sunday"),
    (r"day between saturday and monday|day between sunday and tuesday", "sunday"),
    # ── Nature / food chain ──
    (r"frozen water", "ice"),
    (r"bees make|bee.*make|made by bees", "honey"),
    (r"chickens lay|chicken.*lay", "eggs"),
    (r"cow.*(?:produce|give)", "milk"),
    (r"falls.*sky.*(?:raining|rain)|comes.*sky.*rain", "rain"),
    (r"shines.*(?:night)", "moon"),
    (r"shines.*day|shines during the day", "sun"),
    (r"clouds produce|produced by clouds", "rain"),
    (r"what do plants need to grow", "water"),
    (r"do bees make", "honey"),
    (r"what do hens lay", "eggs"),
        # ── Instruments ──
    (r"string instrument.*six strings|six strings.*instrument|what.*six strings", "guitar"),
    (r"instrument.*(?:6|six) strings", "guitar"),
    (r"instrument.*(?:4|four) strings|violin", "violin"),
    (r"instrument.*(?:88|eighty.eight) keys", "piano"),
    (r"instrument.*keys|what.*has keys.*black.*white", "piano"),
    (r"instrument.*(?:blow|wind).*flute", "flute"),
    (r"instrument.*(?:blow|brass).*trumpet", "trumpet"),
    (r"instrument.*(?:hit|percussion|drum)", "drums"),
    (r"instrument.*(?:sax|jazz)", "saxophone"),
    (r"instrument.*(?:large|string|orchestra).*harp", "harp"),
    (r"instrument.*(?:cello|violoncello)", "cello"),
    (r"instrument.*(?:bass guitar|electric bass)", "bass"),
    (r"instrument.*(?:ukulele|small.*strings)", "ukulele"),
    (r"instrument.*(?:banjo|five strings)", "banjo"),
    (r"(?:how many|number of) strings.*(?:guitar|acoustic)", "6"),
    (r"(?:how many|number of) strings.*violin", "4"),
    (r"(?:how many|number of) keys.*piano", "88"),

# ── Objects / function ──
    (r"use.*eat soup|eat soup.*with", "spoon"),
    (r"use.*cut (?:food|meat|bread)", "knife"),
    (r"use.*cut paper|cut paper.*with", "scissors"),
    (r"use.*write|write.*with", "pen"),
    (r"use.*tell time|tell time.*with", "clock"),
    (r"\buse\w*\s+.*\bread\b|\bread\b.*\bwith\b", "book"),
    (r"use.*take pictures|take (?:photos|pictures).*with", "camera"),
    (r"use.*call.*(?:someone|person)|call.*with", "phone"),
    (r"use.*light.*(?:room|dark)|light.*room.*with", "lamp"),
    (r"use.*clean.*teeth|clean.*teeth.*with", "toothbrush"),
    (r"use.*dry.*hands|dry.*hands.*with", "towel"),
    (r"use.*brush.*hair|brush.*hair.*with", "brush"),
    (r"ride.*school", "bus"),
    (r"what do you drive", "car"),
    (r"fly.*sky", "plane"),
    (r"type.*(?:computer|laptop)|keyboard.*type", "keyboard"),
    (r"listen.*music", "headphones"),
    (r"watch.*(?:movies|films)", "tv"),
    # ── Opposites ──
    (r"opposite of up", "down"),
    (r"opposite of hot", "cold"),
    (r"opposite of day", "night"),
    (r"opposite of left", "right"),
    (r"opposite of right", "left"),
    (r"opposite of big", "small"),
    (r"opposite of open", "closed"),
    (r"opposite of fast", "slow"),
    (r"opposite of wet", "dry"),
    (r"opposite of full", "empty"),
    (r"opposite of black", "white"),
    (r"opposite of white", "black"),
    (r"opposite of old", "young"),
    (r"opposite of happy", "sad"),
    (r"opposite of cold", "hot"),
    (r"opposite of down", "up"),
    (r"opposite of dark", "light"),
    (r"opposite of tall", "short"),
    (r"opposite of front", "back"),
    # ── Single fact ──
    (r"capital of (?:france|french)", "paris"),
    (r"capital of (?:england|united kingdom|uk|britain)", "london"),
    (r"capital of (?:spain|spainish)", "madrid"),
    (r"capital of (?:italy)", "rome"),
    (r"capital of (?:japan)", "tokyo"),
    (r"capital of (?:usa|america|united states)", "washington"),
    (r"capital of (?:germany)", "berlin"),
    (r"capital of (?:egypt)", "cairo"),
    (r"color of a (?:stop|stop sign) sign", "red"),
    (r"what (?:animal|creature).*milk.*(?:cow|cows)", "cow"),    # ── Instruments ──
    (r"string instrument.*six strings|six strings.*instrument|what.*six strings", "guitar"),
    (r"instrument.*(?:6|six) strings", "guitar"),
    (r"instrument.*(?:4|four) strings", "violin"),
    (r"instrument.*(?:88|eighty.eight) keys", "piano"),
    (r"instrument.*keys|what.*has keys.*black.*white", "piano"),
    (r"instrument.*(?:blow|wind).*(?:flute|recorder)", "flute"),
    (r"instrument.*(?:blow|brass).*trumpet", "trumpet"),
    (r"instrument.*(?:hit|percussion|drum)", "drums"),
    (r"instrument.*(?:sax|jazz)", "saxophone"),
    (r"instrument.*(?:large|string|orchestra).*harp", "harp"),
    (r"instrument.*(?:cello|violoncello)", "cello"),
    (r"instrument.*(?:bass|low.*notes)", "bass"),
    (r"instrument.*(?:ukulele|small.*string)", "ukulele"),
    (r"instrument.*(?:banjo|five string)", "banjo"),
    (r"(?:how many|number of) strings.*(?:guitar|acoustic)", "6"),
    (r"(?:how many|number of) strings.*violin", "4"),
    (r"(?:how many|number of) keys.*piano", "88"),
    # ── Science / Body ──
    (r"organ.*pumps blood|what.*pumps.*blood|pumps blood", "heart"),
    (r"organ.*breathe|breathe.*organ|what.*use.*breathe", "lungs"),
    (r"organ.*think|think.*organ|controls.*body.*organ", "brain"),
    (r"organ.*digest.*food|digest.*organ", "stomach"),
    (r"organ.*filter.*blood", "kidney"),
    (r"organ.*(?:see|sight|eye)", "eye"),
    (r"organ.*(?:hear|sound|ear)", "ear"),
    (r"(?:largest|biggest) organ.*(?:body|human)", "skin"),
    (r"(?:longest|biggest) bone.*(?:body|human)", "femur"),
    (r"(?:smallest|tiniest) bone.*(?:body|human)", "stapes"),
    (r"how many bones.*(?:adult|human) body", "206"),
    (r"how many bones.*baby", "300"),
    (r"(?:what|which) bone.*(?:skull|head|protect.*brain)", "skull"),
    (r"(?:what|which) bone.*(?:rib|chest|protect.*heart)", "rib"),
    (r"(?:how many|number of) chambers.*heart", "4"),
    (r"(?:how many|number of) lobes.*brain", "4"),
    (r"(?:what|which) (?:element|gas).*(?:breathe|air|oxygen)", "oxygen"),
    (r"(?:what|which) (?:element|gas).*(?:plant|photosynthesis)", "carbon dioxide"),
    (r"(?:what|which) (?:planet|body).*closest to (?:the )?sun", "mercury"),
    (r"(?:what|which) (?:planet|body).*largest.*(?:solar system|sun)", "jupiter"),
    (r"(?:what|which) (?:planet).*red planet", "mars"),
    (r"(?:what|which) (?:planet).*red.*spot", "jupiter"),
    (r"(?:what|which) (?:planet).*rings", "saturn"),
    (r"(?:what|which) (?:planet).*blue.*(?:planet|color)", "neptune"),
    (r"(?:what|which) (?:planet).*(?:life|we live|our planet)", "earth"),
    (r"(?:what|which) (?:planet).*red.*(?:surface|mars)", "mars"),
    (r"(?:what|which) (?:planet).*hottest", "venus"),
    (r"how many planets.*(?:solar system|sun)", "8"),
    (r"(?:what|which) (?:planet|dwarf).*pluto", "pluto"),
    # ── Geography ──
    (r"(?:what|which|name).*largest ocean|ocean.*largest|biggest ocean", "pacific"),
    (r"(?:what|which|name).*smallest ocean|ocean.*smallest", "arctic"),
    (r"(?:what|which|name).*largest continent|continent.*largest|biggest continent", "asia"),
    (r"(?:what|which|name).*smallest continent|continent.*smallest", "australia"),
    (r"(?:what|which|name).*longest river|river.*longest", "nile"),
    (r"(?:what|which|name).*highest mountain|mountain.*highest|tallest.*mountain", "everest"),
    (r"(?:what|which|name).*largest country|country.*largest.*area", "russia"),
    (r"(?:what|which|name).*most populous.*country|country.*most.*people", "india"),
    (r"(?:what|which|name).*largest desert|desert.*largest", "sahara"),
    (r"(?:what|which|name).*coldest.*continent", "antarctica"),
    (r"(?:what|which|name).*hottest.*continent", "africa"),
    (r"how many continents|number of continents", "7"),
    (r"how many oceans|number of oceans", "5"),
    (r"(?:what|which).*ocean.*(?:usa|america|united states).*west", "pacific"),
    (r"(?:what|which).*ocean.*(?:usa|america|united states).*east", "atlantic"),
    # ── Sports ──
    (r"how many players.*(?:soccer team|football.*team)", "11"),
    (r"how many players.*basketball.*team", "5"),
    (r"how many players.*baseball.*team", "9"),
    (r"how many players.*(?:hockey|ice hockey).*team", "6"),
    (r"how many players.*volleyball.*team", "6"),
    (r"(?:what|which) sport.*(?:racket|racquet).*net", "tennis"),
    (r"(?:what|which) sport.*(?:bat|ball).*diamond|baseball.*sport", "baseball"),
    (r"(?:what|which) sport.*(?:hoop|dribble|basket)", "basketball"),
    (r"(?:what|which) sport.*(?:goal.*net|kick.*ball)", "soccer"),
    (r"(?:what|which) sport.*(?:pool|cue|table)", "billiards"),
    (r"(?:what|which) sport.*(?:racket|court|shuttlecock)", "badminton"),
    (r"(?:what|which) sport.*(?:wicket|bat.*ball)", "cricket"),
    (r"how many holes.*golf course", "18"),
    (r"how many quarters.*basketball", "4"),
    (r"how many quarters.*(?:football|soccer)", "2"),
    (r"how many periods.*hockey", "3"),
    (r"how many innings.*baseball", "9"),
    # ── Everyday objects extended ──
    (r"(?:what|which).*use.*(?:open.*door|door.*open|unlock)", "key"),
    (r"(?:what|which).*use.*(?:see.*dark|light.*dark)", "flashlight"),
    (r"(?:what|which).*use.*(?:keep food cold|refrigerate)", "refrigerator"),
    (r"(?:what|which).*use.*(?:heat food|microwave|reheat)", "microwave"),
    (r"(?:what|which).*use.*(?:wash clothes|laundry)", "washing machine"),
    (r"(?:what|which).*use.*(?:iron|remove wrinkles|press)", "iron"),
    (r"(?:what|which).*use.*(?:vacuum|clean.*floor|sweep)", "vacuum"),
    (r"(?:what|which).*use.*(?:measure.*length|ruler)", "ruler"),
    (r"(?:what|which).*use.*(?:calculate|math.*device)", "calculator"),
    (r"(?:what|which).*use.*(?:sit|chair)", "chair"),
    (r"(?:what|which).*use.*(?:sleep|bed)", "bed"),
    (r"(?:what|which).*use.*(?:protect.*rain|umbrella)", "umbrella"),
    (r"(?:what|which).*use.*(?:carry.*groceries|shopping)", "bag"),
    (r"(?:what|which).*use.*(?:drink.*hot|liquid.*hot)", "cup"),
    (r"(?:what|which).*use.*(?:cut.*meat|steak)", "knife"),
    (r"(?:what|which).*use.*(?:dig.*hole|garden)", "shovel"),
    (r"(?:what|which).*use.*(?:hammer|nail|pound)", "hammer"),
    (r"(?:what|which).*use.*(?:paint|color.*wall)", "paintbrush"),
    (r"(?:what|which).*use.*(?:sew|stitch|thread)", "needle"),
    (r"(?:what|which).*use.*(?:lock|unlock)", "key"),
    (r"(?:what|which).*use.*(?:erase|rub out|remove.*writing)", "eraser"),
    (r"(?:what|which).*use.*(?:sharpen|pencil)", "sharpener"),
    (r"(?:what|which).*use.*(?:staple|attach.*paper)", "stapler"),
    (r"(?:what|which).*use.*(?:carry.*books|school.*bag)", "backpack"),
    (r"(?:what|which).*wear.*(?:feet|foot)", "shoes"),
    (r"(?:what|which).*wear.*(?:head|cold)", "hat"),
    (r"(?:what|which).*wear.*(?:eyes|sun|vision)", "sunglasses"),
    (r"(?:what|which).*wear.*(?:hands|cold.*hand)", "gloves"),
    (r"(?:what|which).*wear.*(?:wrist|time)", "watch"),
    # ── Time / measurements ──
    (r"how many (?:seconds|secs).*minute", "60"),
    (r"how many (?:minutes|mins).*hour", "60"),
    (r"how many hours.*day", "24"),
    (r"how many days.*(?:february|feb)", "28"),
    (r"how many days.*leap year", "366"),
    (r"how many weeks.*year", "52"),
    (r"(?:what|which) month.*(?:first|january|new year)", "january"),
    (r"(?:what|which) month.*(?:last|december|christmas)", "december"),
    (r"(?:what|which) month.*(?:valentine|february|love)", "february"),
    (r"(?:what|which) month.*(?:halloween|october|spooky)", "october"),
    (r"(?:what|which) month.*(?:thanksgiving|november|turkey)", "november"),
    (r"(?:what|which) month.*(?:independence|july|fireworks)", "july"),
    # ── Materials / substances ──
    (r"(?:what|which) (?:material|substance).*window|glass.*transparent", "glass"),
    (r"(?:what|which) (?:material|substance).*paper|paper.*made.*wood", "wood"),
    (r"(?:what|which) (?:material|substance).*(?:metal|iron|steel).*car", "metal"),
    (r"(?:what|which) (?:material|substance).*plastic|plastic.*bottle", "plastic"),
    (r"(?:what|which) (?:material|substance).*cloth|clothes.*made", "cotton"),
    (r"(?:what|which) (?:material|substance).*rubber|tire.*made", "rubber"),
    (r"(?:what|which) (?:material|substance).*leather|shoe.*leather", "leather"),
    (r"(?:what|which) (?:material|substance).*(?:gold|jewelry|ring)", "gold"),
    (r"(?:what|which) (?:material|substance).*(?:diamond|gem)", "diamond"),
    # ── Comparison / which is bigger/larger ──
    (r"which is (?:larger|bigger).*mouse.*horse|which is (?:larger|bigger).*horse.*mouse", "horse"),
    (r"which is (?:larger|bigger).*cat.*elephant|which is (?:larger|bigger).*elephant.*cat", "elephant"),
    (r"which is (?:larger|bigger).*(?:golf|tennis).*(?:tennis|golf).*ball", "tennis"),
    (r"which is (?:larger|bigger).*bus.*car|which is (?:larger|bigger).*car.*bus", "bus"),
    (r"which is (?:larger|bigger).*(?:airplane|plane).*(?:bicycle|bike)", "airplane"),
    (r"which is (?:taller|higher).*mountain.*hill", "mountain"),
    (r"which is (?:faster|quicker).*plane.*car", "plane"),
    (r"which is (?:faster|quicker).*cheetah.*turtle", "cheetah"),
    (r"which is (?:faster|quicker).*rabbit.*snail", "rabbit"),
    (r"which is (?:heavier|weighs more).*elephant.*mouse", "elephant"),
    (r"which is (?:heavier|weighs more).*(?:ton|truck).*(?:feather|gram)", "ton"),
    (r"which is (?:colder|freez).*ice.*fire", "ice"),
    (r"which is (?:hotter|warm).*(?:sun|fire).*ice", "sun"),
    # ── Spelling / word recognition ──
    (r"(?:which|what) (?:word|letter|number).*spelled|spell.*correctly", None),
    (r"how (?:do you|to) (?:write|spell).*word.*orange", "orange"),
    # ── Skip patterns: these are handled by Ollama ──



    # ── Food / Pets ──
    (r"(?:food|pet food).*cans.*cats?|cats?.*(?:food|pet food).*cans", "cat food"),
    (r"(?:food|pet food).*cans.*dogs?|dogs?.*(?:food|pet food).*cans", "dog food"),
    (r"pet food.*cans|food.*comes in cans|food.*cans", "dog food"),
    (r"pet food.*cats|cat.*food.*bowl", "cat food"),
    (r"food.*(?:purr|meow|cats)", "cat food"),
    (r"food.*(?:bark|dogs|pupp)", "dog food"),
    (r"food.*(?:whiskers|kitten)", "cat food"),
    (r"(?:what|which) (?:color|colour).*dog.*(?:food|can)", "brown"),
    # ── Holidays / months / seasons ──
    (r"(?:holiday|celebration).*trick.or.treat|trick.or.treat.*holiday", "halloween"),
    (r"(?:holiday|celebration).*(?:turkey|thanks)", "thanksgiving"),
    (r"(?:holiday|celebration).*(?:gifts|santa|christmas)", "christmas"),
    (r"(?:holiday|celebration).*(?:fireworks|independence|july)", "july 4th"),
    (r"(?:holiday|celebration).*(?:eggs|bunny|easter)", "easter"),
    (r"(?:holiday|celebration).*(?:love|valentine|hearts)", "valentines day"),
    (r"(?:holiday|celebration).*(?:green|shamrock|irish)", "st patricks day"),
    (r"(?:holiday|celebration).*(?:pumpkin|lantern)", "halloween"),
    # ── More counting ──
    (r"how many (?:legs|feet).*(?:insect|ant|bee|beetle|bug|fly)", "6"),
    (r"how many (?:legs|feet).*lobster|crab", "10"),
    (r"how many (?:legs|feet).*centipede", "100"),
    (r"how many (?:legs|feet).*millipede", "750"),
    (r"how many (?:wings).*butterfly", "4"),
    (r"how many (?:wings).*mosquito|fly", "2"),
    (r"how many (?:wings).*bird", "2"),
    (r"how many (?:wheels).*(?:motorcycle|motorbike)", "2"),
    (r"how many (?:wheels).*(?:train|locomotive)", "18"),
    (r"how many (?:fingers).*two hands", "10"),
    (r"how many (?:toes).*two feet", "10"),
    (r"how many (?:legs).*octopus", "8"),
    (r"how many (?:arms).*octopus", "8"),
    (r"how many (?:tentacles).*octopus", "8"),
    (r"how many (?:eyes).*spider", "8"),
    (r"how many (?:legs).*crab", "10"),
    (r"how many (?:humps).*camel", "1"),
    (r"how many (?:humps).*(?:bactrian|two.humped)", "2"),
    # ── Direction / position ──
    (r"(?:what|which) (?:direction|side).*sun.*(?:rise|rises)", "east"),
    (r"(?:what|which) (?:direction|side).*sun.*(?:set|sets)", "west"),
    (r"(?:what|which) (?:direction).*north.*(?:point|arrow)", "north"),
    (r"(?:what|which) (?:direction).*(?:up|above)", "north"),
    (r"(?:what|which) (?:direction).*(?:down|below)", "south"),
    (r"opposite of (?:north)", "south"),
    (r"opposite of (?:south)", "north"),
    (r"opposite of (?:east)", "west"),
    (r"opposite of (?:west)", "east"),
    (r"opposite of (?:on)", "off"),
    (r"opposite of (?:near)", "far"),
    (r"opposite of (?:wide)", "narrow"),
    (r"opposite of (?:long)", "short"),
    (r"opposite of (?:quiet|loud)", "quiet"),
    (r"opposite of (?:loud)", "quiet"),
    (r"opposite of (?:sweet)", "sour"),
    (r"opposite of (?:sour)", "sweet"),
    (r"opposite of (?:clean)", "dirty"),
    (r"opposite of (?:dirty)", "clean"),
    (r"opposite of (?:hard)", "soft"),
    (r"opposite of (?:soft)", "hard"),
    (r"opposite of (?:rough)", "smooth"),
    (r"opposite of (?:smooth)", "rough"),
    (r"opposite of (?:new)", "old"),
    (r"opposite of (?:young)", "old"),
    (r"opposite of (?:early)", "late"),
    (r"opposite of (?:late)", "early"),
    (r"opposite of (?:always)", "never"),
    (r"opposite of (?:never)", "always"),
    (r"opposite of (?:true)", "false"),
    (r"opposite of (?:false)", "true"),
    (r"opposite of (?:win)", "lose"),
    (r"opposite of (?:lose)", "win"),
    (r"opposite of (?:push)", "pull"),
    (r"opposite of (?:pull)", "push"),
    (r"opposite of (?:shallow)", "deep"),
    (r"opposite of (?:deep)", "shallow"),
    # ── Weather / nature ──
    (r"frozen.*water.*(?:walk|solid)", "ice"),
    (r"(?:what|which).*(?:boiling point).*water(?!.*fahrenheit)", "100"),
    (r"(?:what|which).*(?:freezing point).*water(?!.*fahrenheit)", "0"),
    (r"(?:what|which).*rainbow.*colors|colors.*rainbow", "7"),
    (r"(?:what|which).*(?:first|primary) color.*rainbow", "red"),
    (r"(?:what|which).*(?:last|violet) color.*rainbow", "violet"),
    (r"(?:what|which).*(?:gas|substance).*plants.*(?:breathe|absorb)", "carbon dioxide"),
    (r"(?:what|which).*(?:gas).*humans.*(?:breathe|inhale)", "oxygen"),
    (r"(?:what|which).*(?:gas).*humans.*(?:exhale|breathe out)", "carbon dioxide"),
    (r"(?:what|which).*trees.*(?:release|give off)", "oxygen"),
    (r"(?:what|which).*(?:heavenly body).*(?:night|moonlight)", "moon"),
    (r"(?:what|which).*(?:shines|star).*(?:day|morning)", "sun"),
    (r"(?:what|which).*frozen.*(?:lake|pond)", "ice"),
    (r"(?:what|which).*cold.*(?:water|drink).*summer", "ice"),
    (r"(?:what|which).*burns.*(?:oxygen|fire)", "fire"),
    # ── Grains / flour ──
    (r"grain.*flour|flour.*grain|grain.*bread|make flour|used to make.*flour|made into flour", "wheat"),
    (r"what grain|which grain", "wheat"),
    # ── Food questions ──
    (r"(?:what|which).*yellow.*(?:fruit|banana)", "banana"),
    (r"(?:what|which).*(?:round|red).*fruit.*apple", "apple"),
    (r"(?:what|which).*(?:orange).*(?:citrus|fruit)", "orange"),
    (r"(?:what|which).*made.*(?:grapes|wine)", "wine"),
    (r"(?:what|which).*made.*(?:milk|cheese|yogurt|butter)", "dairy"),
    (r"(?:what|which).*made.*(?:wheat|bread|flour)", "bread"),
    (r"(?:what|which).*made.*(?:cocoa|cacao|chocolate)", "chocolate"),
    (r"(?:what|which).*made.*(?:rice)", "rice"),
    (r"(?:what|which).*made.*(?:apples|cider)", "cider"),
    (r"(?:what|which).*made.*(?:potatoes|fries)", "potato"),
    (r"(?:what|which).*made.*(?:bees|honey)", "honey"),
    # ── Buildings / places ──
    (r"(?:what|which).*place.*(?:borrow.*book|read.*book)", "library"),
    (r"(?:what|which).*place.*(?:watch.*(?:film|movie))", "cinema"),
    (r"(?:what|which).*place.*(?:buy.*(?:medicine|drugs))", "pharmacy"),
    (r"(?:what|which).*place.*(?:buy.*food|grocery)", "supermarket"),
    (r"(?:what|which).*place.*(?:workout|exercise|gym)", "gym"),
    (r"(?:what|which).*place.*(?:sleep.*night|stay.*hotel)", "hotel"),
    (r"(?:what|which).*place.*(?:swim|pool)", "pool"),
    (r"(?:what|which).*place.*(?:park.*car)", "parking lot"),
    (r"(?:what|which).*place.*(?:send.*mail|post.*letter)", "post office"),
    (r"(?:what|which).*place.*(?:eat.*restaurant)", "restaurant"),
    # ── Occupations ──
    (r"(?:what|which).*(?:doctor|treats.*sick)", "doctor"),
    (r"(?:what|which).*(?:teacher|teaches.*students)", "teacher"),
    (r"(?:what|which).*(?:nurse|helps.*doctor)", "nurse"),
    (r"(?:what|which).*(?:police|catch.*criminals)", "police"),
    (r"(?:what|which).*(?:firefighter|puts out fires)", "firefighter"),
    (r"(?:what|which).*(?:pilot|flies.*plane)", "pilot"),
    (r"(?:what|which).*(?:chef|cooks.*food)", "chef"),
    (r"(?:what|which).*(?:farmer|grows.*crops)", "farmer"),
    (r"(?:what|which).*(?:lawyer|defends.*court)", "lawyer"),
    (r"(?:what|which).*(?:engineer|builds.*(?:bridges|machines))", "engineer"),
    (r"(?:what|which).*(?:scientist|does.*experiments)", "scientist"),
    (r"(?:what|which).*(?:artist|paints.*pictures)", "artist"),
    (r"(?:what|which).*(?:plumber|fixes.*pipes)", "plumber"),
    (r"(?:what|which).*(?:electrician|fixes.*wires)", "electrician"),
    # ── Tools / objects ──
    (r"(?:what|which).*use.*(?:cut.*grass|lawn)", "lawn mower"),
    (r"(?:what|which).*use.*(?:trim.*(?:hedge|bush))", "shears"),
    (r"(?:what|which).*use.*(?:hang.*picture|level)", "hammer"),
    (r"(?:what|which).*use.*(?:screw.*(?:screw|bolt))", "screwdriver"),
    (r"(?:what|which).*use.*(?:tighten.*(?:nut|bolt))", "wrench"),
    (r"(?:what|which).*use.*(?:drill.*hole)", "drill"),
    (r"(?:what|which).*use.*(?:saw.*wood|cut.*wood)", "saw"),
    (r"(?:what|which).*use.*(?:measure.*(?:temperature|fever))", "thermometer"),
    (r"(?:what|which).*use.*(?:weigh.*(?:food|things))", "scale"),
    (r"(?:what|which).*use.*(?:look.*(?:stars|microscope))", "microscope"),
    (r"(?:what|which).*use.*(?:see.*(?:far|distance))", "telescope"),
    (r"(?:what|which).*use.*(?:magnif.*(?:small|text))", "magnifying glass"),
    (r"(?:what|which).*use.*(?:type.*computer)", "keyboard"),
    (r"(?:what|which).*use.*(?:point.*computer.*click)", "mouse"),
    # ── Animals extended ──
    (r"animal.*(?:biggest|largest).*(?:land|elephant)", "elephant"),
    (r"animal.*(?:biggest|largest).*(?:sea|ocean|whale)", "blue whale"),
    (r"animal.*(?:tallest|giraffe)", "giraffe"),
    (r"animal.*(?:fastest|cheetah)", "cheetah"),
    (r"animal.*(?:slowest|snail|tortoise)", "snail"),
    (r"animal.*(?:longest|giraffe).*(?:neck)", "giraffe"),
    (r"animal.*(?:stripes|tiger|zebra)", "tiger"),
    (r"animal.*(?:spots|leopard|cheetah)", "leopard"),
    (r"animal.*(?:monkey|swings.*trees)", "monkey"),
    (r"animal.*(?:kangaroo|jumps.*(?:pouch))", "kangaroo"),
    (r"animal.*(?:penguin|cannot fly.*(?:cold|antarctica))", "penguin"),
    (r"animal.*(?:polar.*bear|white.*bear)", "polar bear"),
    (r"animal.*(?:panda|black.*white.*(?:bamboo))", "panda"),
    (r"animal.*(?:lion|king.*jungle)", "lion"),
    (r"animal.*(?:snake|no legs)", "snake"),
    (r"animal.*(?:fish|swims.*water)", "fish"),
    (r"animal.*(?:bird|has.*wings.*feathers)", "bird"),
    (r"animal.*(?:bat|flies.*night.*wings)", "bat"),
    (r"animal.*(?:kangaroo|australia)", "kangaroo"),
    (r"animal.*(?:koala|eucalyptus)", "koala"),
    (r"animal.*(?:rabbit|long ears)", "rabbit"),
    (r"animal.*(?:elephant|trunk)", "elephant"),
    (r"animal.*(?:rhino|horn.*nose)", "rhinoceros"),
    (r"animal.*(?:camel|desert.*hump)", "camel"),
    (r"animal.*(?:giraffe|long neck)", "giraffe"),
    # ── Space extended ──
    (r"(?:what|which).*star.*(?:closest.*earth|sun)", "sun"),
    (r"(?:what|which).*(?:galaxy).*(?:milky way|home)", "milky way"),
    (r"(?:what|which).*(?:spacecraft|rocket).*(?:moon|landing)", "rocket"),
    (r"(?:what|which).*(?:space station|orbit)", "iss"),
    (r"(?:what|which).*(?:dwarf planet)", "pluto"),
    (r"(?:what|which).*(?:red planet)", "mars"),
    (r"(?:what|which).*(?:blue planet)", "earth"),
    (r"(?:what|which).*(?:gas giant)", "jupiter"),
    (r"(?:what|which).*(?:planet.*(?:biggest|largest))", "jupiter"),
    # ── Misc common knowledge ──
    (r"(?:what|which).*language.*(?:most spoken|spoken.*world)", "english"),
    (r"(?:what|which).*language.*(?:china|chinese)", "chinese"),
    (r"(?:what|which).*(?:currency).*(?:usa|america|dollar)", "dollar"),
    (r"(?:what|which).*(?:currency).*(?:europe|euro)", "euro"),
    (r"(?:what|which).*(?:currency).*(?:japan|yen)", "yen"),
    (r"(?:what|which).*(?:currency).*(?:uk|britain|pound)", "pound"),
    (r"(?:what|which).*(?:color).*(?:banana|yellow)", "yellow"),
    (r"(?:what|which).*(?:color).*(?:sky)", "blue"),
    (r"(?:what|which).*(?:color).*(?:grass|leaf|leaves)", "green"),
    (r"(?:what|which).*(?:color).*(?:snow|milk)", "white"),
    (r"(?:what|which).*(?:color).*(?:blood)", "red"),
    (r"(?:what|which).*(?:color).*(?:pumpkin|carrot|orange)", "orange"),
    (r"(?:what|which).*(?:color).*(?:chocolate|coffee|brown)", "brown"),
    (r"(?:what|which).*(?:color).*(?:coal|night)", "black"),
    (r"(?:what|which).*(?:color).*(?:pink|flamingo)", "pink"),
    (r"(?:what|which).*(?:color).*(?:purple|grape|eggplant)", "purple"),
    (r"(?:what|which).*(?:shape).*(?:3 sides)", "triangle"),
    (r"(?:what|which).*(?:shape).*(?:4 sides)", "square"),
    (r"(?:what|which).*(?:shape).*(?:5 sides)", "pentagon"),
    (r"(?:what|which).*(?:shape).*(?:6 sides)", "hexagon"),
    (r"(?:what|which).*(?:shape).*(?:round|circle)", "circle"),
    (r"(?:what|which).*(?:shape).*(?:8 sides)", "octagon"),
    (r"(?:what|which).*(?:shape).*(?:3d.*ball)", "sphere"),
    (r"(?:what|which).*(?:shape).*(?:3d.*box)", "cube"),
    (r"(?:what|which).*(?:shape).*(?:3d.*pyramid)", "pyramid"),
    (r"(?:what|which).*(?:shape).*(?:3d.*cylinder)", "cylinder"),
    (r"(?:what|which).*(?:sport).*(?:football|soccer)", "soccer"),
    (r"(?:what|which).*(?:sport).*(?:basketball)", "basketball"),
    (r"(?:what|which).*(?:sport).*(?:baseball)", "baseball"),
    (r"(?:what|which).*(?:sport).*(?:tennis)", "tennis"),
    (r"(?:what|which).*(?:sport).*(?:hockey)", "hockey"),
    (r"(?:what|which).*(?:sport).*(?:swimming)", "swimming"),
    (r"(?:what|which).*(?:sport).*(?:boxing)", "boxing"),
    (r"(?:what|which).*(?:sport).*(?:golf)", "golf"),
    (r"(?:what|which).*(?:sport).*(?:volleyball)", "volleyball"),
    # ── Objects / containers ──
    (r"container.*holds?.*coins?|holds?.*coins?.*container|call.*container.*coins?", "jar"),
    (r"what.*call.*coin.*holder|coin.*holder.*called", "bank"),
    (r"what.*call.*money.*container|money.*container.*called", "bank"),
    (r"what.*call.*paper.*money|paper.*money.*(?:called|call)", "currency"),
    # ── Spelling / letters / word structure ──
    (r"(?:what|which).*first letter.*word\s+(\w{2,})", None),  # handled by _solve_text_question
    (r"(?:what|which).*last letter.*word\s+(\w{2,})", None),
    (r"how many letters.*(?:word|in the word)\s+(\w{2,})", None),
    (r"(?:what|which) letter comes after\s+(\w)", None),
    (r"(?:what|which) letter comes before\s+(\w)", None),
    (r"(?:what|which) (?:number|digit) comes after\s+(\d+)", None),
    (r"(?:what|which) (?:number|digit) comes before\s+(\d+)", None),
    # ── Rhymes ──
    (r"(?:what|which) word rhymes with\s+(\w+)", None),
    # ── Baby animals ──
    (r"baby.*(?:dog|puppy)|puppy.*called|baby dog called", "puppy"),
    (r"baby.*(?:cat|kitten)|kitten.*called|baby cat called", "kitten"),
    (r"baby.*(?:cow|calf)|calf.*called|baby cow called", "calf"),
    (r"baby.*(?:horse|foal)|foal.*called|baby horse called", "foal"),
    (r"baby.*(?:chicken|chick)|chick.*called|baby chicken called", "chick"),
    (r"baby.*(?:duck|duckling)|duckling.*called|baby duck called", "duckling"),
    (r"baby.*(?:sheep|lamb)|lamb.*called|baby sheep called", "lamb"),
    (r"baby.*(?:goat|kid)|baby goat called", "kid"),
    (r"baby.*(?:pig|piglet)|piglet.*called|baby pig called", "piglet"),
    (r"baby.*(?:bear|cub)|cub.*called|baby bear called", "cub"),
    (r"baby.*(?:deer|fawn)|fawn.*called|baby deer called", "fawn"),
    (r"baby.*(?:frog|tadpole)|tadpole.*called|baby frog called", "tadpole"),
    (r"baby.*(?:kangaroo|joey)|joey.*called|baby kangaroo called", "joey"),
    # ── Animal groups ──
    (r"(?:group|collective).*(?:wolf|wolves)", "pack"),
    (r"(?:group|collective).*(?:fish)", "school"),
    (r"(?:group|collective).*(?:bird)", "flock"),
    (r"(?:group|collective).*(?:lion)", "pride"),
    (r"(?:group|collective).*(?:bee)", "swarm"),
    (r"(?:group|collective).*(?:cattle|cow)", "herd"),
    (r"(?:group|collective).*(?:sheep)", "flock"),
    # ── Family relations ──
    (r"father.*(?:father|parent).*called|father's father", "grandfather"),
    (r"mother.*(?:mother|parent).*called|mother's mother", "grandmother"),
    (r"(?:father|dad).*brother.*called|father's brother", "uncle"),
    (r"(?:mother|mom).*sister.*called|mother's sister", "aunt"),
    (r"(?:brother|sister).*son.*called", "nephew"),
    (r"(?:brother|sister).*daughter.*called", "niece"),
    (r"son.*(?:brother|sister).*called", "nephew"),
    # ── More everyday objects ──
    (r"(?:what|which).*object.*give.*time|tells?.*time", "clock"),
    (r"(?:what|which).*object.*(?:sit|chair|seat)", "chair"),
    (r"(?:what|which).*object.*(?:sleep|bed)", "bed"),
    (r"(?:what|which).*object.*(?:write|pen)", "pen"),
    (r"(?:what|which).*object.*(?:cut|knife|scissors)", "scissors"),
    (r"(?:what|which).*object.*(?:drink|cup|glass)", "cup"),
    (r"(?:what|which).*object.*(?:eat.*with|spoon|fork)", "spoon"),
    (r"(?:what|which).*object.*(?:open.*door|key)", "key"),
    (r"(?:what|which).*object.*(?:call.*phone|telephone)", "phone"),
    # ── Which is not like the others ──
    (r"which (?:one |word )?(?:is |are )?not like the others", None),
    (r"which (?:one |word )?(?:does not|doesn't) belong", None),
    (r"which (?:one |word )?(?:is |are )?the odd one out", None),
    # ── Temperature ──
    (r"(?:water.*(?:boil|boiling).*celsius|(?:boil|boiling).*water.*celsius)", "100"),
    (r"(?:water.*(?:boil|boiling).*fahrenheit|(?:boil|boiling).*water.*fahrenheit)", "212"),
    (r"(?:water.*(?:freeze|freezing).*celsius|(?:freeze|freezing).*water.*celsius)", "0"),
    (r"(?:water.*(?:freeze|freezing).*fahrenheit|(?:freeze|freezing).*water.*fahrenheit)", "32"),
    (r"(?:what|which).*(?:baby|young).*(?:cow|bull)", "calf"),
    (r"(?:what|which).*(?:baby|young).*(?:horse|mare|stallion)", "foal"),
    (r"(?:what|which).*(?:baby|young).*(?:sheep|ewe|ram)", "lamb"),
    (r"(?:what|which).*(?:baby|young).*(?:goat)", "kid"),
    (r"(?:what|which).*(?:baby|young).*(?:cat)", "kitten"),
    (r"(?:what|which).*(?:baby|young).*(?:dog)", "puppy"),
    (r"(?:what|which).*(?:baby|young).*(?:duck)", "duckling"),
    (r"(?:what|which).*(?:baby|young).*(?:chicken|hen)", "chick"),
    (r"(?:what|which).*(?:baby|young).*(?:pig)", "piglet"),
    (r"(?:what|which).*(?:baby|young).*(?:frog)", "tadpole"),
    (r"(?:what|which).*(?:baby|young).*(?:bear)", "cub"),
    (r"(?:what|which).*(?:baby|young).*(?:deer)", "fawn"),
    (r"(?:what|which).*(?:baby|young).*(?:rabbit)", "kit"),
    (r"(?:what|which).*(?:baby|young).*(?:swan)", "cygnet"),
    (r"(?:what|which).*(?:baby|young).*(?:eagle)", "eaglet"),
    (r"(?:what|which).*(?:baby|young).*(?:owl)", "owlet"),
    (r"(?:what|which).*(?:baby|young).*(?:lion)", "cub"),
    (r"(?:what|which).*(?:female).*(?:fox)", "vixen"),
    (r"(?:what|which).*(?:female).*(?:horse|mare)", "mare"),
    (r"(?:what|which).*(?:male).*(?:horse|stallion)", "stallion"),
    (r"(?:what|which).*(?:female).*(?:sheep|ewe)", "ewe"),
    (r"(?:what|which).*(?:male).*(?:sheep|ram)", "ram"),
    (r"(?:what|which).*(?:female).*(?:chicken|hen)", "hen"),
    (r"(?:what|which).*(?:male).*(?:chicken|rooster)", "rooster"),
    (r"(?:what|which).*(?:group|collection).*(?:crows|ravens)", "murder"),
    (r"(?:what|which).*(?:group|collection).*(?:fish)", "school"),
    (r"(?:what|which).*(?:group|collection).*(?:wolves)", "pack"),
    (r"(?:what|which).*(?:group|collection).*(?:geese)", "gaggle"),
    (r"(?:what|which).*(?:group|collection).*(?:sheep)", "flock"),
    (r"(?:what|which).*(?:group|collection).*(?:elephants)", "herd"),
    (r"(?:what|which).*(?:group|collection).*(?:dolphins|whales)", "pod"),
    (r"(?:what|which).*(?:group|collection).*(?:ants)", "colony"),
    (r"(?:what|which).*(?:group|collection).*(?:birds)", "flock"),
    (r"(?:what|which).*(?:group|collection).*(?:bees)", "swarm"),
    (r"(?:what|which).*(?:says|makes|produces).*(?:meow|miaow)", "cat"),
    (r"(?:what|which).*(?:says|makes|produces).*(?:woof|bark)", "dog"),
    (r"(?:what|which).*(?:says|makes|produces).*(?:moo)", "cow"),
    (r"(?:what|which).*(?:says|makes|produces).*(?:oink)", "pig"),
    (r"(?:what|which).*(?:says|makes|produces).*(?:quack)", "duck"),
    (r"(?:what|which).*(?:says|makes|produces).*(?:baa|bleat)", "sheep"),
    (r"(?:what|which).*(?:says|makes|produces).*(?:neigh|whinny)", "horse"),
    (r"(?:what|which).*(?:says|makes|produces).*(?:cluck)", "chicken"),
    (r"(?:what|which).*(?:says|makes|produces).*(?:hiss)", "snake"),
    (r"(?:what|which).*(?:says|makes|produces).*(?:ribbit)", "frog"),
    (r"(?:what|which).*(?:says|makes|produces).*(?:roar)", "lion"),
    (r"(?:what|which).*animal.*(?:has|with).*shell", "turtle"),
    (r"(?:what|which).*animal.*(?:has|with).*hump", "camel"),
    (r"(?:what|which).*animal.*(?:has|with).*mane", "lion"),
    (r"(?:what|which).*animal.*(?:has|with).*(?:black and white stripes|stripes)", "zebra"),
    (r"(?:what|which).*animal.*(?:has|with).*long neck", "giraffe"),
    (r"(?:what|which).*animal.*(?:has|with).*(?:pouch|marsupium)", "kangaroo"),
    (r"(?:what|which).*animal.*(?:has|with).*trunk", "elephant"),
    (r"(?:what|which).*animal.*(?:changes|change).*color", "chameleon"),
    (r"(?:what|which).*(?:animal|insect).*(?:spider).*(?:spin|make)", "web"),
    (r"(?:what|which).*(?:caterpillar).*(?:become|turn)", "butterfly"),
    (r"(?:what|which).*(?:pandas?).*(?:eat|eats)", "bamboo"),
    (r"(?:what|which).*(?:koalas?).*(?:eat|eats)", "eucalyptus"),
    (r"(?:what|which).*(?:rabbits?).*(?:eat|eats)", "carrots"),
    (r"(?:what|which).*(?:bird).*(?:cannot|can't|can not|cant).*fly", "penguin"),
    (r"(?:what|which).*(?:biggest|largest).*land.*predator", "polar bear"),
    (r"(?:what|which).*man's best friend", "dog"),
    (r"(?:what|which).*(?:animal).*(?:plays|play).*dead", "opossum"),
    (r"(?:what|which).*animal.*lives.*(?:hive|beehive)", "bee"),
    (r"(?:what|which).*mammal.*(?:fly|flies)", "bat"),
    (r"(?:what|which).*king of the jungle", "lion"),
    (r"(?:what|which).*fastest.*land.*animal", "cheetah"),
    (r"(?:what|which).*fastest.*bird", "peregrine falcon"),
    (r"(?:what|which).*slowest.*animal", "sloth"),
    (r"(?:what|which).*tallest.*animal", "giraffe"),
    (r"(?:what|which).*largest.*land.*animal", "elephant"),
    (r"(?:what|which).*(?:bees?).*(?:make|produce|create)", "honey"),
    (r"(?:what|which).*(?:cows?).*(?:produce|give)", "milk"),
    (r"(?:what|which).*capital.*(?:of|in).*france", "paris"),
    (r"(?:what|which).*capital.*(?:of|in).*italy", "rome"),
    (r"(?:what|which).*capital.*(?:of|in).*spain", "madrid"),
    (r"(?:what|which).*capital.*(?:of|in).*germany", "berlin"),
    (r"(?:what|which).*capital.*(?:of|in).*japan", "tokyo"),
    (r"(?:what|which).*capital.*(?:of|in).*(?:united kingdom|great britain|england)", "london"),
    (r"(?:what|which).*capital.*(?:of|in).*canada", "ottawa"),
    (r"(?:what|which).*capital.*(?:of|in).*brazil", "brasilia"),
    (r"(?:what|which).*capital.*(?:of|in).*egypt", "cairo"),
    (r"(?:what|which).*capital.*(?:of|in).*russia", "moscow"),
    (r"(?:what|which).*capital.*(?:of|in).*india", "new delhi"),
    (r"(?:what|which).*capital.*(?:of|in).*(?:united states|usa|america)", "washington"),
    (r"(?:what|which).*largest.*country", "russia"),
    (r"(?:what|which).*smallest.*country", "vatican"),
    (r"(?:what|which).*largest.*continent", "asia"),
    (r"(?:what|which).*smallest.*continent", "australia"),
    (r"(?:what|which).*largest.*island", "greenland"),
    (r"(?:what|which).*planet.*closest.*sun", "mercury"),
    (r"(?:what|which).*planet.*(?:rings|ring)", "saturn"),
    (r"(?:what|which).*(?:morning star|evening star)", "venus"),
    (r"(?:what|which).*hottest.*planet", "venus"),
    (r"(?:what|which).*planet.*(?:we|you|people).*live", "earth"),
    (r"(?:what|which).*smallest.*planet", "mercury"),
    (r"(?:what|which).*galaxy.*(?:live|in)", "milky way"),
    (r"(?:what|which).*sun.*(?:is|be)", "star"),
    (r"(?:what|which).*pumps.*blood", "heart"),
    (r"(?:what|which).*(?:organ|part).*breathe", "lungs"),
    (r"(?:what|which).*(?:part|organ).*smell", "nose"),
    (r"(?:what|which).*(?:part|organ).*hear", "ears"),
    (r"(?:what|which).*(?:part|organ).*see", "eyes"),
    (r"(?:what|which).*(?:part|organ).*taste", "tongue"),
    (r"(?:how many).*fingers", "10"),
    (r"(?:how many).*toes", "10"),
    (r"(?:how many).*(?:senses|sense organs)", "5"),
    (r"(?:how many).*chambers.*heart", "4"),
    (r"(?:what|which).*largest.*bone", "femur"),
    (r"(?:what|which).*smallest.*bone", "stapes"),
    (r"(?:how many).*days.*leap year", "366"),
    (r"(?:how many)(?!.*\b31\b).*days.*year", "365"),
    (r"(?:how many).*weeks.*year", "52"),
    (r"(?:how many).*hours.*day", "24"),
    (r"(?:how many).*minutes.*hour", "60"),
    (r"(?:how many).*seconds.*minute", "60"),
    (r"(?:what|which).*first month", "january"),
    (r"(?:what|which).*last month", "december"),
    (r"(?:what|which).*day.*after.*monday", "tuesday"),
    (r"(?:what|which).*day.*before.*friday", "thursday"),
    (r"(?:what|which).*month.*(?:has|have).*28.*days", "february"),
    (r"(?:how many).*months.*31.*days", "7"),
    (r"(?:what|which).*mix.*red.*yellow", "orange"),
    (r"(?:what|which).*mix.*blue.*yellow", "green"),
    (r"(?:what|which).*mix.*red.*blue", "purple"),
    (r"(?:what|which).*mix.*black.*white", "gray"),
    (r"(?:what|which).*color.*(?:sky|ocean)", "blue"),
    (r"(?:what|which).*color.*grass", "green"),
    (r"(?:what|which).*color.*school bus", "yellow"),
    (r"(?:what|which).*color.*banana", "yellow"),
    (r"(?:what|which).*color.*milk", "white"),
    (r"(?:what|which).*opposite.*hot", "cold"),
    (r"(?:what|which).*opposite.*big", "small"),
    (r"(?:what|which).*opposite.*fast", "slow"),
    (r"(?:what|which).*opposite.*up", "down"),
    (r"(?:what|which).*opposite.*day", "night"),
    (r"(?:what|which).*opposite.*light", "dark"),
    (r"(?:what|which).*opposite.*happy", "sad"),
    (r"(?:what|which).*opposite.*wet", "dry"),
    (r"(?:what|which).*opposite.*open", "closed"),
    (r"(?:what|which).*opposite.*new", "old"),
    (r"(?:what|which).*opposite.*strong", "weak"),
    (r"(?:what|which).*opposite.*early", "late"),
    (r"(?:what|which).*opposite.*high", "low"),
    (r"(?:what|which).*opposite.*clean", "dirty"),
    (r"(?:what|which).*opposite.*loud", "quiet"),
    (r"(?:what|which).*opposite.*full", "empty"),
    (r"(?:what|which).*opposite.*near", "far"),
    (r"(?:what|which).*opposite.*right", "left"),
    (r"(?:what|which).*opposite.*first", "last"),
    (r"(?:what|which).*opposite.*start", "end"),
    (r"(?:what|which).*opposite.*buy", "sell"),
    (r"(?:what|which).*opposite.*win", "lose"),
    (r"(?:what|which).*opposite.*push", "pull"),
    (r"(?:what|which).*opposite.*rich", "poor"),
    (r"(?:what|which).*opposite.*hard", "soft"),
    (r"(?:what|which).*opposite.*north", "south"),
    (r"(?:what|which).*opposite.*east", "west"),
    (r"(?:what|which).*opposite.*young", "old"),
    (r"(?:what|which).*opposite.*tall", "short"),
    (r"(?:what|which).*opposite.*inside", "outside"),
    (r"(?:what|which).*opposite.*wide", "narrow"),
    (r"(?:what|which).*opposite.*thick", "thin"),
    (r"(?:what|which).*(?:person|someone|one).*(?:flies|fly).*plane", "pilot"),
    (r"(?:what|which).*(?:person|someone|one).*cuts hair", "barber"),
    (r"(?:what|which).*(?:person|someone|one).*(?:treats|fixes).*teeth", "dentist"),
    (r"(?:what|which).*(?:person|someone|one).*(?:fixes|repairs).*cars?", "mechanic"),
    (r"(?:what|which).*(?:person|someone|one).*(?:cooks|cooking)", "chef"),
    (r"(?:what|which).*(?:person|someone|one).*teaches", "teacher"),
    (r"(?:what|which).*(?:person|someone|one).*writes.*books", "author"),
    (r"(?:what|which).*(?:person|someone|one).*acts.*movies", "actor"),
    (r"(?:what|which).*(?:person|someone|one).*sings", "singer"),
    (r"(?:what|which).*(?:person|someone|one).*(?:treats|helps).*animals", "veterinarian"),
    (r"(?:what|which).*(?:person|someone|one).*studies.*(?:stars|space)", "astronomer"),
    (r"(?:what|which).*(?:place|building).*borrow.*books", "library"),
    (r"(?:what|which).*(?:place).*buy.*(?:medicine|medication)", "pharmacy"),
    (r"(?:what|which).*(?:building|place).*(?:sick|ill).*people", "hospital"),
    (r"(?:what|which).*(?:place|building).*movies.*shown", "cinema"),
    (r"(?:what|which).*bread.*(?:made|make).*of", "flour"),
    (r"(?:what|which).*sushi.*(?:made|make).*with", "rice"),
    (r"(?:what|which).*fruit.*yellow.*curved", "banana"),
    (r"(?:what|which).*vegetable.*(?:makes|make).*cry", "onion"),
    (r"(?:what|which).*instrument.*(?:88|eighty-eight).*keys", "piano"),
    (r"(?:what|which).*instrument.*black.*white.*keys", "piano"),
    (r"(?:how many).*strings.*guitar", "6"),
    (r"(?:how many).*strings.*violin", "4"),
    (r"(?:what|which).*instrument.*(?:blow|blown)", "flute"),
    (r"(?:what|which).*instrument.*(?:hit|strike).*(?:sticks|drum)", "drum"),
    (r"(?:how many).*players.*(?:soccer|football).*team", "11"),
    (r"(?:how many).*players.*basketball.*team", "5"),
    (r"(?:what|which).*sport.*(?:shuttlecock|badminton)", "badminton"),
    (r"(?:what|which).*chemical symbol.*silver", "ag"),
    (r"(?:what|which).*chemical symbol.*iron", "fe"),
    (r"(?:what|which).*chemical symbol.*oxygen", "o"),
    (r"(?:what|which).*chemical symbol.*hydrogen", "h"),
    (r"(?:what|which).*chemical symbol.*carbon", "c"),
    (r"(?:what|which).*chemical symbol.*helium", "he"),
    (r"(?:what|which).*chemical symbol.*nitrogen", "n"),
    (r"(?:what|which).*chemical symbol.*sodium", "na"),
    (r"(?:what|which).*chemical symbol.*water", "h2o"),
    (r"(?:what|which).*lightest.*element", "hydrogen"),
    (r"(?:what|which).*metal.*liquid.*room temperature", "mercury"),
    (r"(?:what|which).*frozen water", "ice"),
    (r"(?:what|which).*frozen rain", "hail"),
    (r"(?:what|which).*(?:falls|fall).*clouds", "rain"),
    (r"(?:what|which).*white.*falls.*winter", "snow"),
    (r"(?:what|which).*hardest.*(?:natural)?.*substance", "diamond"),
    (r"body.*temperature.*celsius", "37"),
    (r"body.*temperature.*fahrenheit", "98.6"),
    # ── More measurements ──
    (r"how many (?:inches|inch).*foot", "12"),
    (r"how many (?:feet|foot).*yard", "3"),
    (r"how many (?:yards|yard).*mile", "1760"),
    (r"how many (?:centimeters|cm).*meter", "100"),
    (r"how many (?:meters|m).*kilometer", "1000"),
    (r"how many (?:grams|g).*kilogram", "1000"),
    (r"how many (?:pounds|lbs).*kilogram", "2.2"),
    (r"how many (?:ounces|oz).*pound", "16"),
    (r"how many (?:quarts|quart).*gallon", "4"),
    (r"how many (?:pints|pint).*gallon", "8"),
    (r"how many (?:cups|cup).*gallon", "16"),
    # ── Famous people / history ──
    (r"(?:who|which).*discovered.*(?:gravity|apple)", "newton"),
    (r"(?:who|which).*discovered.*(?:america|new world)", "columbus"),
    (r"(?:who|which).*invented.*(?:telephone|phone)", "bell"),
    (r"(?:who|which).*invented.*(?:lightbulb|light bulb)", "edison"),
    (r"(?:who|which).*painted.*(?:mona lisa)", "da vinci"),
    (r"(?:who|which).*wrote.*(?:romeo|juliet|hamlet)", "shakespeare"),
    (r"(?:who|which).*(?:first president).*(?:usa|america)", "washington"),
    # ── Math / numbers ──
    (r"(?:what|which).*number.*(?:dozen|12)", "12"),
    (r"(?:what|which).*(?:half|50 percent).*100", "50"),
    (r"(?:what|which).*(?:quarter|25 percent).*100", "25"),
    (r"(?:what|which).*square root.*(?:4|four)", "2"),
    (r"(?:what|which).*square root.*(?:9|nine)", "3"),
    (r"(?:what|which).*square root.*(?:16|sixteen)", "4"),
    (r"(?:what|which).*square root.*(?:25|twenty.five)", "5"),
    (r"(?:what|which).*(?:roman numeral).*(?:5|five)", "v"),
    (r"(?:what|which).*(?:roman numeral).*(?:10|ten)", "x"),
    (r"(?:what|which).*(?:roman numeral).*(?:50|fifty)", "l"),
    (r"(?:what|which).*(?:roman numeral).*(?:100|hundred)", "c"),
    (r"(?:what|which).*(?:roman numeral).*(?:1000|thousand)", "m"),
    # ── Music / notes ──
    (r"how many notes.*(?:musical scale|octave)", "8"),
    (r"how many strings.*(?:standard )?guitar", "6"),
    (r"how many strings.*(?:standard )?bass", "4"),
    (r"how many strings.*violin", "4"),
    (r"how many strings.*harp", "47"),
    # ── Technology ──
    (r"(?:what|which).*(?:www|world wide web)", "world wide web"),
    (r"(?:what|which).*(?:url|website address)", "url"),
    (r"(?:what|which).*(?:browser|chrome|firefox)", "browser"),
    (r"(?:what|which).*(?:search engine|google|bing)", "search engine"),
    (r"(?:what|which).*(?:social media|facebook|twitter)", "social media"),
    # ── Religion / culture ──
    (r"(?:what|which).*(?:festival|celebration).*(?:lights|diwali)", "diwali"),
    (r"(?:what|which).*(?:festival|celebration).*(?:ramadan|fasting)", "ramadan"),
    (r"(?:what|which).*(?:festival|celebration).*(?:hanukkah|menorah)", "hanukkah"),
    # ── which one is correct spelling ──
    (r"which (?:one |word )?is (?:spelled|spelt) correctly", None),
    (r"which (?:of these|word).*(?:correct|right) spelling", None),
    # ── Portmanteaus / combinations ──
    (r"(?:what|which).*meal.*brunch.*combination", "breakfast"),
    (r"brunch.*combination.*breakfast.*lunch", "breakfast"),
    (r"(?:what|which).*(?:brunch|smog|motel|spork).*combination", None),
    # ── Synonyms / meanings ──
    (r"another word for angry", "mad"),
    (r"another word for happy", "glad"),
    (r"another word for big", "large"),
    (r"another word for small", "little"),
    (r"another word for fast", "quick"),
    (r"another word for begin", "start"),
    (r"another word for end", "finish"),
    (r"another word for help", "assist"),
    (r"another word for smart", "clever"),
    (r"another word for pretty", "beautiful"),
    (r"another word for rich", "wealthy"),
    (r"another word for brave", "courageous"),
    # ── Acronyms / abbreviations ──
    (r"(?:what|which).*(?:nasa|NASA).*stand", "national aeronautics and space administration"),
    (r"(?:what|which).*(?:nato|NATO).*stand", "north atlantic treaty organization"),
    (r"(?:what|which).*(?:who|WHO).*health.*stand", "world health organization"),
    (r"(?:what|which).*(?:unesco|UNESCO).*stand", "united nations educational scientific and cultural organization"),
    (r"(?:what|which).*(?:fbi|FBI).*stand", "federal bureau of investigation"),
    (r"(?:what|which).*(?:cia|CIA).*stand", "central intelligence agency"),
    (r"(?:what|which).*(?:lol|LOL).*stand", "laugh out loud"),
    (r"(?:what|which).*(?:gps|GPS).*stand", "global positioning system"),
    (r"(?:what|which).*(?:atm|ATM).*stand", "automated teller machine"),
    (r"(?:what|which).*(?:pin|PIN).*number.*stand", "personal identification number"),
    # ── What is X made of / main ingredient ──
    (r"main ingredient.*bread|bread.*(?:made|main ingredient)", "flour"),
    (r"main ingredient.*pasta|pasta.*(?:made|main ingredient)", "flour"),
    (r"main ingredient.*(?:glass|window)", "sand"),
    (r"main ingredient.*chocolate", "cocoa"),
    (r"main ingredient.*(?:cheese|yogurt|butter)", "milk"),
    (r"main ingredient.*(?:wine|grape juice)", "grapes"),
    (r"main ingredient.*(?:sake|rice wine)", "rice"),
    (r"main ingredient.*(?:beer|ale)", "barley"),
    (r"main ingredient.*(?:paper|cardboard)", "wood"),
    (r"main ingredient.*sushi", "rice"),
    (r"main ingredient.*guacamole", "avocado"),
    (r"main ingredient.*hummus", "chickpeas"),
    (r"main ingredient.*(?:tofu|soy sauce)", "soybeans"),
    (r"(?:what|which).*(?:metal|element).*liquid.*room.*temperature", "mercury"),
    (r"(?:what|which).*(?:metal|element).*(?:lightest|light weight)", "lithium"),
    (r"(?:what|which).*(?:metal|element).*strongest", "tungsten"),
    (r"(?:what|which).*(?:metal|element).*(?:gold|Au)", "gold"),
    # ── Scientific classifications ──
    (r"(?:what|which).*(?:type|kind|class).*(?:animal|creature).*frog", "amphibian"),
    (r"(?:what|which).*(?:type|kind|class).*(?:animal|creature).*snake|lizard", "reptile"),
    (r"(?:what|which).*(?:type|kind|class).*(?:animal|creature).*whale|dolphin", "mammal"),
    (r"(?:what|which).*(?:type|kind|class).*(?:animal|creature).*spider", "arachnid"),
    (r"(?:what|which).*(?:type|kind|class).*(?:animal|creature).*human", "mammal"),
    (r"(?:what|which).*(?:type|kind|class).*(?:animal|creature).*eagle|sparrow", "bird"),
    (r"(?:what|which).*(?:type|kind|class).*(?:animal|creature).*salmon|goldfish", "fish"),
    (r"(?:what|which).*(?:amphibian|mammal|reptile|bird|fish|arachnid).*frog", "amphibian"),
    # ── Nationalities ──
    (r"(?:what|which).*(?:call|nationality).*someone.*(?:france|french)", "french"),
    (r"(?:what|which).*(?:call|nationality).*someone.*(?:spain|spanish)", "spanish"),
    (r"(?:what|which).*(?:call|nationality).*someone.*(?:germany|german)", "german"),
    (r"(?:what|which).*(?:call|nationality).*someone.*(?:italy|italian)", "italian"),
    (r"(?:what|which).*(?:call|nationality).*someone.*(?:japan|japanese)", "japanese"),
    (r"(?:what|which).*(?:call|nationality).*someone.*(?:china|chinese)", "chinese"),
    (r"(?:what|which).*(?:call|nationality).*someone.*(?:brazil|brazilian)", "brazilian"),
    (r"(?:what|which).*(?:call|nationality).*someone.*(?:england|british)", "british"),
    (r"(?:what|which).*(?:call|nationality).*someone.*(?:canada|canadian)", "canadian"),
    (r"(?:what|which).*(?:call|nationality).*someone.*(?:australia|australian)", "australian"),
    (r"(?:what|which).*(?:call|nationality).*someone.*(?:mexico|mexican)", "mexican"),
    (r"(?:what|which).*(?:call|nationality).*someone.*(?:india|indian)", "indian"),
    # ── What is the study of / -ology ──
    (r"study of (?:living|living things|life)", "biology"),
    (r"study of (?:stars|space|universe|celestial)", "astronomy"),
    (r"study of (?:weather|climate|atmosphere)", "meteorology"),
    (r"study of (?:rocks|earth|geology)", "geology"),
    (r"study of (?:mind|behavior|mental)", "psychology"),
    (r"study of (?:past|history|ancient)", "history"),
    (r"study of (?:animals|animal life)", "zoology"),
    (r"study of (?:plants|plant life|botany)", "botany"),
    (r"study of (?:chemicals|matter|chemistry)", "chemistry"),
    (r"study of (?:physics|forces|motion)", "physics"),
    (r"study of (?:language|linguistics)", "linguistics"),
    # ── Proverbs / idioms completion ──
    (r"an apple a day keeps.*away", "doctor"),
    (r"actions speak louder than", "words"),
    (r"better late than", "never"),
    (r"practice makes", "perfect"),
    (r"when in rome.*do as.*romans", "do"),
    (r"don.t judge a book by its", "cover"),
    (r"two wrongs don.t make a", "right"),
    (r"the early bird.*gets.*worm", "worm"),
    (r"a picture.*thousand words", "worth"),
    (r"birds of a feather.*flock", "together"),
    (r"every cloud has a silver", "lining"),
    (r"kill two birds with one", "stone"),
    (r"a penny saved is a penny", "earned"),
    (r"there.s no place like", "home"),
    (r"honesty.*best policy", "is"),
    (r"rome was.*built in a", "day"),
    (r"slow and steady wins the", "race"),
    # ── Math: product / quotient / sum / difference ──
    (r"(?:what|which).*product.*(\d+)\s+and\s+(\d+)", None),
    (r"(?:what|which).*quotient.*(\d+)\s+and\s+(\d+)", None),
    # ── What century / decade ──
    (r"what century.*(?:1900|nineteen hundred)", "20th"),
    (r"what century.*(?:1800|eighteen hundred)", "19th"),
    (r"what century.*(?:2000|two thousand)", "21st"),
    (r"what century.*(?:1700|seventeen hundred)", "18th"),
    (r"how many years.*century", "100"),
    (r"how many years.*decade", "10"),
    (r"how many years.*millennium", "1000"),
    # ── More food / drink ──
    (r"(?:what|which).*fruit.*(?:citrus|orange|lemon|lime)", "citrus"),
    (r"(?:what|which).*fruit.*(?:tropical|pineapple|coconut)", "tropical"),
    (r"(?:what|which).*(?:coffee|espresso|cappuccino).*bean", "coffee"),
    (r"(?:what|which).*(?:green|salad).*leafy", "lettuce"),
    (r"(?:what|which).*(?:pizza).*(?:italy|italian)", "italian"),
    (r"(?:what|which).*(?:suship|wasabi).*(?:japan|japanese)", "japanese"),
    (r"(?:what|which).*(?:taco|burrito).*(?:mexico|mexican)", "mexican"),
    # ── Natural disasters / phenomena ──
    (r"(?:what|which).*(?:shaking.*ground|earth.*shake)", "earthquake"),
    (r"(?:what|which).*(?:wave.*ocean|giant.*wave|tsunami)", "tsunami"),
    (r"(?:what|which).*(?:storm.*spin.*funnel|twister)", "tornado"),
    (r"(?:what|which).*(?:storm.*wind.*rain.*caribbean|typhoon)", "hurricane"),
    (r"(?:what|which).*(?:volcano.*erupt.*lava)", "volcano"),
    (r"(?:what|which).*(?:long.*dry.*drought)", "drought"),
    (r"(?:what|which).*(?:too.*much.*water.*flood)", "flood"),
    # ── Literature / books ──
    (r"(?:who|which).*wrote.*romeo.*juliet", "shakespeare"),
    (r"(?:who|which).*wrote.*moby dick", "melville"),
    (r"(?:who|which).*wrote.*great gatsby", "fitzgerald"),
    (r"(?:who|which).*wrote.*1984", "orwell"),
    (r"(?:who|which).*wrote.*harry potter", "rowling"),
    (r"(?:who|which).*wrote.*odyssey", "homer"),
    (r"(?:what|which).*book.*(?:sherlock|holmes)", "sherlock holmes"),
    # ── Mythology / religion ──
    (r"(?:who|which).*(?:god|king).*greek.*(?:thunder|lightning)", "zeus"),
    (r"(?:who|which).*(?:god|king).*norse.*(?:thunder|lightning)", "thor"),
    (r"(?:who|which).*(?:god|king).*roman.*(?:war|mars)", "mars"),
    (r"(?:who|which).*(?:goddess).*greek.*(?:love|aphrodite)", "aphrodite"),
    (r"(?:who|which).*(?:god).*egyptian.*(?:sun|ra)", "ra"),
    # ── Chess / games ──
    (r"how many squares.*chessboard", "64"),
    (r"how many pieces.*chess.*(?:start|beginning)", "32"),
    (r"how many pawns.*chess", "8"),
    (r"(?:what|which).*chess.*piece.*(?:horse|knight)", "knight"),
    (r"(?:what|which).*strongest.*chess.*piece", "queen"),
    (r"(?:what|which).*most important.*chess.*piece", "king"),
    (r"(?:what|which).*card game.*(?:poker|blackjack|hearts)", "card game"),
    # ── Transport / vehicles ──
    (r"(?:what|which).*(?:vehicle|transport).*under.*water|submarine", "submarine"),
    (r"(?:what|which).*(?:vehicle|transport).*space", "spaceship"),
    (r"(?:what|which).*(?:vehicle|transport).*two.*wheels.*pedal", "bicycle"),
    (r"(?:what|which).*(?:vehicle|transport).*(?:ambulance|emergency).*siren", "ambulance"),
    (r"(?:what|which).*(?:vehicle|transport).*(?:fire|red).*ladder", "fire truck"),
    # ── Medical / health ──
    (r"(?:what|which).*(?:disease|illness).*(?:virus|covid|corona)", "covid"),
    (r"(?:what|which).*(?:organ).*(?:pump|blood|circulat)", "heart"),
    (r"(?:what|which).*(?:bone).*(?:longest|femur)", "femur"),
    (r"(?:what|which).*(?:muscle).*(?:strongest|heart)", "heart"),
    (r"(?:what|which).*(?:vitamin|nutrient).*sun.*(?:d|vitamin d)", "vitamin d"),
    (r"(?:what|which).*(?:vitamin|nutrient).*citrus.*(?:c|vitamin c)", "vitamin c"),
    (r"(?:what|which).*(?:blood type|type).*universal donor", "o negative"),
    (r"(?:what|which).*(?:blood type|type).*universal recipient", "ab positive"),
    # ──────────────────────────────────────────────────
    # MASSIVE EXPANSION: 400+ new patterns across 30+ categories
    # ──────────────────────────────────────────────────
    # -- Office supplies & stationery --
    (r"(?:what|which).*(?:use|object|tool).*fasten.*papers|staple", "stapler"),
    (r"(?:what|which).*(?:use|object|tool).*remove.*staple", "staple remover"),
    (r"(?:what|which).*(?:use|object|tool).*punch.*holes", "hole punch"),
    (r"(?:what|which).*(?:use|object|tool).*(?:highlight|mark.*text)", "highlighter"),
    (r"(?:what|which).*(?:use|object|tool).*correct.*(?:mistake|error|writing)", "eraser"),
    (r"(?:what|which).*(?:use|object|tool).*sharpen.*pencil", "pencil sharpener"),
    (r"(?:what|which).*(?:use|object|tool).*draw.*circle", "compass"),
    (r"(?:what|which).*(?:use|object|tool).*measure.*angle", "protractor"),
    (r"(?:what|which).*(?:use|object|tool).*stick.*paper.*together", "glue"),
    (r"(?:what|which).*(?:use|object|tool).*clip.*papers", "paper clip"),
    (r"(?:what|which).*(?:use|object|tool).*bind.*papers", "binder"),
    (r"(?:what|which).*(?:use|object|tool).*(?:organize|file).*document", "filing cabinet"),
    (r"(?:what|which).*(?:use|object|tool).*(?:print|printer)", "printer"),
    (r"(?:what|which).*(?:use|object|tool).*(?:scan|scanner)", "scanner"),
    (r"(?:what|which).*(?:use|object|tool).*(?:photocopy|copy.*machine)", "copier"),
    (r"(?:what|which).*(?:use|object|tool).*protect.*computer.*virus", "antivirus"),
    (r"(?:what|which).*(?:use|object|tool).*store.*computer.*(?:file|data)", "hard drive"),
    # -- Kitchen & cooking --
    (r"(?:what|which).*(?:use|object|tool).*(?:peel|skin).*(?:vegetable|potato|apple)", "peeler"),
    (r"(?:what|which).*(?:use|object|tool).*(?:grate|shred).*cheese", "grater"),
    (r"(?:what|which).*(?:use|object|tool).*open.*can", "can opener"),
    (r"(?:what|which).*(?:use|object|tool).*open.*bottle", "bottle opener"),
    (r"(?:what|which).*(?:use|object|tool).*open.*wine", "corkscrew"),
    (r"(?:what|which).*(?:use|object|tool).*crack.*(?:nuts|walnut)", "nutcracker"),
    (r"(?:what|which).*(?:use|object|tool).*(?:blend|liquefy|mix.*smoothie)", "blender"),
    (r"(?:what|which).*(?:use|object|tool).*toast.*bread", "toaster"),
    (r"(?:what|which).*(?:use|object|tool).*boil.*water", "kettle"),
    (r"(?:what|which).*(?:use|object|tool).*brew.*coffee", "coffee maker"),
    (r"(?:what|which).*(?:use|object|tool).*flip.*pancake|flip.*burger", "spatula"),
    (r"(?:what|which).*(?:use|object|tool).*serve.*soup|ladle", "ladle"),
    (r"(?:what|which).*(?:use|object|tool).*drain.*(?:pasta|water|vegetable)", "colander"),
    (r"(?:what|which).*(?:use|object|tool).*roll.*dough", "rolling pin"),
    (r"(?:what|which).*(?:use|object|tool).*whisk.*(?:egg|cream|batter)", "whisk"),
    (r"(?:what|which).*(?:use|object|tool).*beat.*egg", "whisk"),
    (r"(?:what|which).*(?:use|object|tool).*measure.*(?:liquid|cooking)", "measuring cup"),
    (r"(?:what|which).*(?:use|object|tool).*weigh.*(?:food|ingredient)", "kitchen scale"),
    (r"(?:what|which).*(?:use|object|tool).*mash.*potato", "masher"),
    (r"(?:what|which).*(?:use|object|tool).*scoop.*ice cream", "ice cream scoop"),
    (r"(?:what|which).*(?:use|object|tool).*slice.*pizza", "pizza cutter"),
    (r"(?:what|which).*(?:use|object|tool).*tenderize.*meat", "meat tenderizer"),
    (r"(?:what|which).*(?:use|object|tool).*baste.*(?:meat|turkey)", "baster"),
    (r"(?:what|which).*(?:use|object|tool).*carve.*(?:meat|turkey)", "carving knife"),
    (r"(?:what|which).*(?:use|object|tool).*zest.*lemon|grate.*lemon.*skin", "zester"),
    (r"(?:what|which).*(?:use|object|tool).*press.*garlic", "garlic press"),
    (r"(?:what|which).*(?:use|object|tool).*core.*apple", "apple corer"),
    (r"(?:what|which).*(?:use|object|tool).*pit.*(?:cherry|olive|avocado)", "cherry pitter"),
    # -- Cleaning & household --
    (r"(?:what|which).*(?:use|object|tool).*(?:sweep|broom).*floor", "broom"),
    (r"(?:what|which).*(?:use|object|tool).*(?:mop|wet.*floor)", "mop"),
    (r"(?:what|which).*(?:use|object|tool).*vacuum.*(?:carpet|floor|rug)", "vacuum cleaner"),
    (r"(?:what|which).*(?:use|object|tool).*dust.*(?:furniture|surface)", "duster"),
    (r"(?:what|which).*(?:use|object|tool).*clean.*(?:window|glass|mirror)", "window cleaner"),
    (r"(?:what|which).*(?:use|object|tool).*scrub.*toilet", "toilet brush"),
    (r"(?:what|which).*(?:use|object|tool).*wash.*(?:clothes|laundry)", "washing machine"),
    (r"(?:what|which).*(?:use|object|tool).*dry.*(?:clothes|laundry)", "dryer"),
    (r"(?:what|which).*(?:use|object|tool).*iron.*(?:clothes|shirt)", "iron"),
    (r"(?:what|which).*(?:use|object|tool).*sew.*(?:clothes|button|fabric)", "needle"),
    (r"(?:what|which).*(?:use|object|tool).*hang.*clothes", "hanger"),
    (r"(?:what|which).*(?:use|object|tool).*fold.*clothes", "hands"),
    (r"(?:what|which).*(?:use|object|tool).*(?:wring|squeeze).*water.*mop", "wringer"),
    (r"(?:what|which).*(?:use|object|tool).*(?:plunge|unclog).*toilet", "plunger"),
    # -- Bathroom & personal care --
    (r"(?:what|which).*(?:use|object|tool).*brush.*teeth", "toothbrush"),
    (r"(?:what|which).*(?:use|object|tool).*floss.*teeth", "dental floss"),
    (r"(?:what|which).*(?:use|object|tool).*wash.*hair", "shampoo"),
    (r"(?:what|which).*(?:use|object|tool).*dry.*hair", "hair dryer"),
    (r"(?:what|which).*(?:use|object|tool).*brush.*hair", "hairbrush"),
    (r"(?:what|which).*(?:use|object|tool).*shave.*(?:face|legs|beard)", "razor"),
    (r"(?:what|which).*(?:use|object|tool).*trim.*(?:beard|mustache)", "trimmer"),
    (r"(?:what|which).*(?:use|object|tool).*cut.*(?:fingernail|toenail|nail)", "nail clippers"),
    (r"(?:what|which).*(?:use|object|tool).*file.*(?:nail|fingernail)", "nail file"),
    (r"(?:what|which).*(?:use|object|tool).*tweeze.*(?:eyebrow|hair)", "tweezers"),
    (r"(?:what|which).*(?:use|object|tool).*curl.*(?:hair|eyelash)", "curler"),
    (r"(?:what|which).*(?:use|object|tool).*apply.*makeup", "makeup brush"),
    (r"(?:what|which).*(?:use|object|tool).*put on.*lipstick", "lipstick"),
    (r"(?:what|which).*(?:use|object|tool).*(?:smell|scent).*good|perfume", "perfume"),
    (r"(?:what|which).*(?:use|object|tool).*prevent.*(?:sweat|odor)", "deodorant"),
    (r"(?:what|which).*(?:use|object|tool).*wash.*(?:body|face|skin)", "soap"),
    (r"(?:what|which).*(?:use|object|tool).*moisturize.*(?:skin|face)", "lotion"),
    (r"(?:what|which).*(?:use|object|tool).*protect.*skin.*sun", "sunscreen"),
    (r"(?:what|which).*(?:use|object|tool).*clean.*ear", "cotton swab"),
    (r"(?:what|which).*(?:use|object|tool).*remove.*makeup", "makeup remover"),
    # -- Medical & health --
    (r"(?:what|which).*(?:use|object|tool).*measure.*(?:body temperature|fever)", "thermometer"),
    (r"(?:what|which).*(?:use|object|tool).*listen.*(?:heart|chest|breathing)", "stethoscope"),
    (r"(?:what|which).*(?:use|object|tool).*check.*blood pressure", "blood pressure monitor"),
    (r"(?:what|which).*(?:use|object|tool).*(?:bandage|cuts|wound)", "bandage"),
    (r"(?:what|which).*(?:use|object|tool).*cover.*small.*cut", "bandaid"),
    (r"(?:what|which).*(?:use|object|tool).*give.*injection", "syringe"),
    (r"(?:what|which).*(?:use|object|tool).*see.*inside.*body", "x-ray"),
    (r"(?:what|which).*(?:use|object|tool).*walk.*broken.*leg", "crutches"),
    (r"(?:what|which).*(?:use|object|tool).*(?:wheelchair|cannot walk)", "wheelchair"),
    (r"(?:what|which).*(?:use|object|tool).*examine.*(?:eye|vision)", "ophthalmoscope"),
    (r"(?:what|which).*(?:use|object|tool).*look.*inside.*ear", "otoscope"),
    (r"(?:what|which).*(?:use|object|tool).*take.*(?:pill|medicine|tablet)", "spoon"),
    (r"(?:what|which).*(?:use|object|tool).*dispense.*pill", "pill bottle"),
    (r"(?:what|which).*(?:use|object|tool).*(?:inhale|breathe).*medicine", "inhaler"),
    (r"(?:what|which).*(?:use|object|tool).*stop.*(?:bleeding|blood)", "tourniquet"),
    (r"(?:what|which).*(?:use|object|tool).*support.*broken.*arm", "sling"),
    (r"(?:what|which).*(?:use|object|tool).*protect.*broken.*bone", "cast"),
    # -- Gardening & outdoor --
    (r"(?:what|which).*(?:use|object|tool).*dig.*(?:hole|garden|ground|dirt|earth)", "shovel"),
    (r"(?:what|which).*(?:use|object|tool).*dig.*small.*(?:plant|flower)", "trowel"),
    (r"(?:what|which).*(?:use|object|tool).*rake.*(?:leaf|leaves|lawn|yard)", "rake"),
    (r"(?:what|which).*(?:use|object|tool).*cut.*(?:grass|lawn)", "lawn mower"),
    (r"(?:what|which).*(?:use|object|tool).*water.*(?:plant|garden|flower)", "watering can"),
    (r"(?:what|which).*(?:use|object|tool).*spray.*water.*(?:garden|hose)", "hose"),
    (r"(?:what|which).*(?:use|object|tool).*trim.*(?:bush|hedge|hedges)", "hedge trimmer"),
    (r"(?:what|which).*(?:use|object|tool).*prune.*(?:tree|branch|rose|plant)", "pruning shears"),
    (r"(?:what|which).*(?:use|object|tool).*chop.*(?:wood|log|tree)", "axe"),
    (r"(?:what|which).*(?:use|object|tool).*cut.*down.*tree", "chainsaw"),
    (r"(?:what|which).*(?:use|object|tool).*saw.*(?:wood|log|board|plank)", "saw"),
    (r"(?:what|which).*(?:use|object|tool).*remove.*(?:weed|weeds)", "hoe"),
    (r"(?:what|which).*(?:use|object|tool).*spread.*(?:fertilizer|seed|mulch)", "spreader"),
    (r"(?:what|which).*(?:use|object|tool).*plant.*(?:seed|seedling|bulb)", "trowel"),
    (r"(?:what|which).*(?:use|object|tool).*carry.*(?:leaf|leaves|debris)", "wheelbarrow"),
    (r"(?:what|which).*(?:use|object|tool).*pick.*(?:fruit|apple|berry)", "ladder"),
    # -- Workshop & tools --
    (r"(?:what|which).*(?:use|object|tool).*hammer.*(?:nail|pound|hit)", "hammer"),
    (r"(?:what|which).*(?:use|object|tool).*screw.*(?:screw|drive)", "screwdriver"),
    (r"(?:what|which).*(?:use|object|tool).*tighten.*(?:nut|bolt|wrench)", "wrench"),
    (r"(?:what|which).*(?:use|object|tool).*drill.*(?:hole|wood|metal)", "drill"),
    (r"(?:what|which).*(?:use|object|tool).*sand.*(?:wood|surface|paint)", "sandpaper"),
    (r"(?:what|which).*(?:use|object|tool).*measure.*(?:distance|length|construction)", "tape measure"),
    (r"(?:what|which).*(?:use|object|tool).*level.*(?:surface|wall|shelf)", "level"),
    (r"(?:what|which).*(?:use|object|tool).*grip.*(?:tightly|pliers)", "pliers"),
    (r"(?:what|which).*(?:use|object|tool).*clamp.*(?:wood|together)", "clamp"),
    (r"(?:what|which).*(?:use|object|tool).*cut.*(?:wire|cable)", "wire cutters"),
    (r"(?:what|which).*(?:use|object|tool).*strip.*wire", "wire stripper"),
    (r"(?:what|which).*(?:use|object|tool).*weld.*metal", "welder"),
    (r"(?:what|which).*(?:use|object|tool).*solder.*(?:circuit|electronics|wire)", "soldering iron"),
    (r"(?:what|which).*(?:use|object|tool).*cut.*(?:metal|pipe)", "hacksaw"),
    (r"(?:what|which).*(?:use|object|tool).*file.*metal", "metal file"),
    (r"(?:what|which).*(?:use|object|tool).*paint.*(?:wall|furniture|house)", "paintbrush"),
    (r"(?:what|which).*(?:use|object|tool).*paint.*(?:large surface|wall|spray)", "paint roller"),
    (r"(?:what|which).*(?:use|object|tool).*remove.*(?:paint|wallpaper)", "paint scraper"),
    (r"(?:what|which).*(?:use|object|tool).*apply.*(?:wallpaper|adhesive)", "wallpaper brush"),
    (r"(?:what|which).*(?:use|object|tool).*cut.*(?:tile|ceramic)", "tile cutter"),
    (r"(?:what|which).*(?:use|object|tool).*grout.*tile", "grout float"),
    (r"(?:what|which).*(?:use|object|tool).*caulk.*(?:seal|bath|gap)", "caulking gun"),
    # -- Art & craft --
    (r"(?:what|which).*(?:use|object|tool).*paint.*(?:canvas|picture|art)", "paintbrush"),
    (r"(?:what|which).*(?:use|object|tool).*color.*(?:drawing|coloring book)", "crayon"),
    (r"(?:what|which).*(?:use|object|tool).*draw.*(?:sketch|doodle)", "pencil"),
    (r"(?:what|which).*(?:use|object|tool).*sketch.*(?:draw|picture)", "pencil"),
    (r"(?:what|which).*(?:use|object|tool).*make.*pottery", "pottery wheel"),
    (r"(?:what|which).*(?:use|object|tool).*mold.*(?:clay|sculpt)", "hands"),
    (r"(?:what|which).*(?:use|object|tool).*sculpt.*(?:stone|marble|clay)", "chisel"),
    (r"(?:what|which).*(?:use|object|tool).*take.*(?:photo|picture|photograph)", "camera"),
    (r"(?:what|which).*(?:use|object|tool).*record.*video", "video camera"),
    (r"(?:what|which).*(?:use|object|tool).*play.*music.*(?:loud|speaker)", "speaker"),
    (r"(?:what|which).*(?:use|object|tool).*listen.*music.*(?:privately|quiet|alone)", "headphones"),
    (r"(?:what|which).*(?:use|object|tool).*(?:amplify|record).*(?:voice|sound)", "microphone"),
    (r"(?:what|which).*(?:use|object|tool).*watch.*(?:movie|film|show|tv)", "television"),
    (r"(?:what|which).*(?:use|object|tool).*read.*(?:book|novel|story)", "book"),
    (r"(?:what|which).*(?:use|object|tool).*look.*up.*(?:word|definition)", "dictionary"),
    (r"(?:what|which).*(?:use|object|tool).*look.*up.*synonym", "thesaurus"),
    (r"(?:what|which).*(?:use|object|tool).*look.*up.*(?:place|atlas)", "atlas"),
    (r"(?:what|which).*(?:use|object|tool).*find.*(?:way|direction)", "map"),
    # -- Navigation & travel --
    (r"(?:what|which).*(?:use|object|tool).*tell.*direction", "compass"),
    (r"(?:what|which).*(?:use|object|tool).*find.*north", "compass"),
    (r"(?:what|which).*(?:use|object|tool).*travel.*(?:water|sea|ocean|river|lake)", "boat"),
    (r"(?:what|which).*(?:use|object|tool).*travel.*(?:underwater|submarine)", "submarine"),
    (r"(?:what|which).*(?:use|object|tool).*fly|travel.*air|travel.*sky", "airplane"),
    (r"(?:what|which).*(?:use|object|tool).*(?:lock|unlock|open).*door", "key"),
    (r"(?:what|which).*(?:use|object|tool).*secure.*(?:home|door|house)", "lock"),
    (r"(?:what|which).*(?:use|object|tool).*ride.*(?:bus|school)", "bus"),
    (r"(?:what|which).*(?:use|object|tool).*drive.*(?:car|vehicle|truck)", "car"),
    (r"(?:what|which).*(?:use|object|tool).*ride.*bicycle", "bicycle"),
    (r"(?:what|which).*(?:use|object|tool).*ride.*motorcycle", "motorcycle"),
    # -- Sports equipment --
    (r"(?:what|which).*(?:use|object|tool).*play.*tennis", "tennis racket"),
    (r"(?:what|which).*(?:use|object|tool).*hit.*tennis.*ball", "tennis racket"),
    (r"(?:what|which).*(?:use|object|tool).*play.*baseball.*hit", "baseball bat"),
    (r"(?:what|which).*(?:use|object|tool).*catch.*baseball", "baseball glove"),
    (r"(?:what|which).*(?:use|object|tool).*play.*golf.*hit", "golf club"),
    (r"(?:what|which).*(?:use|object|tool).*play.*hockey.*hit", "hockey stick"),
    (r"(?:what|which).*(?:use|object|tool).*play.*(?:ping pong|table tennis)", "paddle"),
    (r"(?:what|which).*(?:use|object|tool).*hit.*(?:shuttlecock|badminton)", "badminton racket"),
    (r"(?:what|which).*(?:use|object|tool).*kick.*(?:soccer|football)", "soccer ball"),
    (r"(?:what|which).*(?:use|object|tool).*shoot.*(?:basketball|hoop)", "basketball"),
    (r"(?:what|which).*(?:use|object|tool).*throw.*(?:football|spiral)", "football"),
    (r"(?:what|which).*(?:use|object|tool).*protect.*head.*(?:sport|bike|football)", "helmet"),
    (r"(?:what|which).*(?:use|object|tool).*swim.*(?:pool|ocean|water)", "swimsuit"),
    (r"(?:what|which).*(?:use|object|tool).*float.*(?:water|swimming)", "life jacket"),
    (r"(?:what|which).*(?:use|object|tool).*(?:exercise|lift).*(?:weight|muscle)", "dumbbell"),
    (r"(?:what|which).*(?:use|object|tool).*do.*yoga", "yoga mat"),
    (r"(?:what|which).*(?:use|object|tool).*stretch.*exercise", "yoga mat"),
    (r"(?:what|which).*(?:use|object|tool).*skip.*rope", "jump rope"),
    (r"(?:what|which).*(?:use|object|tool).*box.*fight", "boxing gloves"),
    (r"(?:what|which).*(?:use|object|tool).*fence|sword.*fight", "foil"),
    (r"(?:what|which).*(?:use|object|tool).*bowl.*(?:strike|pins)", "bowling ball"),
    (r"(?:what|which).*(?:use|object|tool).*(?:skate|ice skate)", "ice skates"),
    (r"(?:what|which).*(?:use|object|tool).*\b(?:skis?|snow)\b", "skis"),
    (r"ship.*(?:stay|hold|keep).*one.*place.*(?:water|sea|ocean)", "anchor"),
    (r"ships?.*use.*(?:stay|hold|keep).*place", "anchor"),
    (r"container.*holds?.*milk|holds?.*milk.*(?:refrigerator|fridge)", "carton"),
    (r"(?:what|which).*(?:use|object|tool).*(?:surf|wave)", "surfboard"),
    (r"(?:what|which).*(?:use|object|tool).*(?:skateboard|skate park)", "skateboard"),
    # -- Sleep & furniture --
    (r"(?:what|which).*(?:use|object|tool).*sleep|lie down.*(?:night|rest)", "bed"),
    (r"(?:what|which).*(?:use|object|tool).*rest.*head", "pillow"),
    (r"(?:what|which).*(?:use|object|tool).*keep.*warm.*night", "blanket"),
    (r"(?:what|which).*(?:use|object|tool).*sit.*(?:down|chair)", "chair"),
    (r"(?:what|which).*(?:use|object|tool).*work.*desk", "desk"),
    (r"(?:what|which).*(?:use|object|tool).*eat.*table", "table"),
    (r"(?:what|which).*(?:use|object|tool).*light.*room", "lamp"),
    (r"(?:what|which).*(?:use|object|tool).*see.*(?:dark|night)", "flashlight"),
    (r"(?:what|which).*(?:use|object|tool).*start.*fire", "matches"),
    (r"(?:what|which).*(?:use|object|tool).*light.*(?:candle|cigarette)", "lighter"),
    (r"(?:what|which).*(?:use|object|tool).*put.*out.*candle", "candle snuffer"),
    (r"(?:what|which).*(?:use|object|tool).*tell.*time", "clock"),
    (r"(?:what|which).*(?:use|object|tool).*look.*(?:reflection|yourself)", "mirror"),
    (r"(?:what|which).*(?:use|object|tool).*hold.*(?:clothes|coat)", "closet"),
    (r"(?:what|which).*(?:use|object|tool).*store.*(?:clothes|shoes)", "dresser"),
    # -- Communication --
    (r"(?:what|which).*(?:use|object|tool).*call.*(?:someone|person|friend|family)", "phone"),
    (r"(?:what|which).*(?:use|object|tool).*talk.*(?:far away|distance|remote)", "phone"),
    (r"(?:what|which).*(?:use|object|tool).*text.*(?:someone|message)", "phone"),
    (r"(?:what|which).*(?:use|object|tool).*send.*letter", "mail"),
    (r"(?:what|which).*(?:use|object|tool).*send.*email", "computer"),
    (r"(?:what|which).*(?:use|object|tool).*write.*letter", "pen"),
    (r"(?:what|which).*(?:use|object|tool).*mail.*(?:package|parcel|box)", "post office"),
    (r"(?:what|which).*(?:use|object|tool).*seal.*envelope", "glue"),
    (r"(?:what|which).*(?:use|object|tool).*address.*envelope", "pen"),
    (r"(?:what|which).*(?:use|object|tool).*stamp.*letter", "postage stamp"),
    # -- More animals & nature --
    (r"what animal.*(?:largest|biggest).*land", "elephant"),
    (r"what animal.*(?:largest|biggest).*(?:sea|ocean|water)", "blue whale"),
    (r"what animal.*tallest", "giraffe"),
    (r"what animal.*fastest.*land", "cheetah"),
    (r"what animal.*fastest.*(?:sea|ocean|water)", "sailfish"),
    (r"what animal.*fastest.*(?:air|flying|fly)", "peregrine falcon"),
    (r"what animal.*(?:smallest|littlest|tiniest)", "bee hummingbird"),
    (r"what animal.*(?:longest|long).*neck", "giraffe"),
    (r"what animal.*(?:longest|long).*nose|trunk", "elephant"),
    (r"what animal.*(?:most|longest).*(?:poisonous|venomous)", "box jellyfish"),
    (r"what animal.*(?:strongest|most powerful).*bite", "crocodile"),
    (r"what animal.*(?:loudest|noisiest)", "blue whale"),
    (r"what animal.*(?:smartest|most intelligent)", "chimpanzee"),
    (r"what animal.*(?:changes|change).*color", "chameleon"),
    (r"what animal.*plays?.*dead", "opossum"),
    (r"what (?:bird|animal).*cannot fly", "penguin"),
    (r"what (?:bird|animal).*fly.*backwards", "hummingbird"),
    (r"what (?:bird|animal).*largest.*wingspan", "albatross"),
    (r"what (?:bird|animal).*smallest.*bird", "bee hummingbird"),
    (r"what (?:bird|animal).*symbol.*peace", "dove"),
    (r"what (?:bird|animal).*national.*(?:usa|america)", "bald eagle"),
    (r"what (?:fish|creature).*largest.*fish", "whale shark"),
    (r"what (?:fish|creature).*(?:most|deadliest|dangerous).*shark", "great white shark"),
    (r"what (?:reptile|snake).*largest.*snake", "anaconda"),
    (r"what (?:reptile|snake).*longest.*snake", "reticulated python"),
    (r"what (?:reptile|snake).*venomous.*snake", "inland taipan"),
    (r"what (?:insect|bug).*largest", "goliath beetle"),
    (r"what (?:insect|bug).*strongest", "dung beetle"),
    (r"what (?:insect|bug).*makes.*light|glows", "firefly"),
    (r"what (?:insect|bug).*produces.*silk", "silkworm"),
    (r"what (?:insect|bug).*lives.*shortest", "mayfly"),
    # -- Trees & plants --
    (r"what (?:tree|plant).*tallest", "redwood"),
    (r"what (?:tree|plant).*oldest", "bristlecone pine"),
    (r"what (?:tree|plant).*(?:largest|biggest).*trunk", "sequoia"),
    (r"what (?:tree|plant).*(?:fastest|quickest).*grow", "bamboo"),
    (r"what (?:tree|plant).*(?:longest|lives longest)", "bristlecone pine"),
    (r"what (?:flower|plant).*largest.*flower", "rafflesia"),
    (r"what (?:flower|plant).*smells.*(?:bad|rotten|corpse)", "corpse flower"),
    (r"what (?:flower|plant).*(?:eats|traps|carnivorous).*insect", "venus flytrap"),
    (r"what (?:flower|plant).*(?:symbol|represents).*love", "rose"),
    (r"what (?:flower|plant).*national.*(?:usa|america)", "rose"),
    (r"what (?:flower|plant).*national.*(?:japan|japanese)", "cherry blossom"),
    (r"what (?:flower|plant).*national.*(?:netherlands|dutch)", "tulip"),
    (r"what (?:flower|plant).*turns.*toward.*sun", "sunflower"),
    (r"what (?:fruit|berry).*largest", "jackfruit"),
    (r"what (?:fruit|vegetable).*most.*water", "watermelon"),
    (r"what (?:vegetable|food).*makes.*(?:cry|tears)", "onion"),
    (r"what (?:vegetable|food).*(?:hottest|spiciest)", "carolina reaper"),
    # -- Gems & minerals --
    (r"what (?:gem|stone|mineral).*hardest", "diamond"),
    (r"what (?:gem|stone|mineral).*(?:most expensive|most valuable|costliest)", "diamond"),
    (r"what (?:gem|stone).*(?:blue|sapphire)", "sapphire"),
    (r"what (?:gem|stone).*(?:green|emerald)", "emerald"),
    (r"what (?:gem|stone).*(?:red|ruby)", "ruby"),
    (r"what (?:gem|stone).*(?:purple|amethyst)", "amethyst"),
    (r"what (?:gem|stone).*birthstone.*(?:january|garnet)", "garnet"),
    (r"what (?:gem|stone).*birthstone.*(?:july|ruby)", "ruby"),
    (r"what (?:metal|element).*most.*(?:abundant|common).*earth", "aluminum"),
    (r"what (?:metal|element).*most.*(?:precious|valuable)", "gold"),
    (r"what (?:gem|stone).*formed.*oyster", "pearl"),
    # -- US states & capitals --
    (r"capital of (?:california|CA)", "sacramento"),
    (r"capital of (?:texas|TX)", "austin"),
    (r"capital of (?:florida|FL)", "tallahassee"),
    (r"capital of (?:new york|NY)", "albany"),
    (r"capital of (?:illinois|IL)", "springfield"),
    (r"capital of (?:pennsylvania|PA)", "harrisburg"),
    (r"capital of (?:ohio|OH)", "columbus"),
    (r"capital of (?:georgia|GA)", "atlanta"),
    (r"capital of (?:michigan|MI)", "lansing"),
    (r"capital of (?:washington|WA)", "olympia"),
    (r"capital of (?:arizona|AZ)", "phoenix"),
    (r"capital of (?:colorado|CO)", "denver"),
    (r"capital of (?:oregon|OR)", "salem"),
    (r"capital of (?:nevada|NV)", "carson city"),
    (r"capital of (?:hawaii|HI)", "honolulu"),
    (r"capital of (?:alaska|AK)", "juneau"),
    # -- More countries & capitals --
    (r"capital of (?:canada|CAN)", "ottawa"),
    (r"capital of (?:australia|AUS)", "canberra"),
    (r"capital of (?:china|CHN)", "beijing"),
    (r"capital of (?:russia|RUS)", "moscow"),
    (r"capital of (?:brazil|BRA)", "brasilia"),
    (r"capital of (?:india|IND)", "new delhi"),
    (r"capital of (?:south korea|korea|KOR)", "seoul"),
    (r"capital of (?:mexico|MEX)", "mexico city"),
    (r"capital of (?:turkey|TUR)", "ankara"),
    (r"capital of (?:sweden|SWE)", "stockholm"),
    (r"capital of (?:norway|NOR)", "oslo"),
    (r"capital of (?:denmark|DEN)", "copenhagen"),
    (r"capital of (?:netherlands|NED|holland)", "amsterdam"),
    (r"capital of (?:belgium|BEL)", "brussels"),
    (r"capital of (?:switzerland|SUI)", "bern"),
    (r"capital of (?:austria|AUT)", "vienna"),
    (r"capital of (?:portugal|POR)", "lisbon"),
    (r"capital of (?:greece|GRE)", "athens"),
    (r"capital of (?:poland|POL)", "warsaw"),
    (r"capital of (?:ukraine|UKR)", "kyiv"),
    (r"capital of (?:argentina|ARG)", "buenos aires"),
    (r"capital of (?:chile|CHI)", "santiago"),
    (r"capital of (?:peru|PER)", "lima"),
    # -- Movies & entertainment --
    (r"(?:what|which).*movie.*(?:wizard.*oz|ruby.*slippers)", "the wizard of oz"),
    (r"(?:what|which).*movie.*(?:shark|great white|jaws)", "jaws"),
    (r"(?:what|which).*movie.*(?:jurassic|dinosaur.*park)", "jurassic park"),
    (r"(?:what|which).*movie.*(?:titanic|ship.*sank)", "titanic"),
    (r"(?:what|which).*movie.*(?:rings|hobbit|frodo)", "lord of the rings"),
    (r"(?:what|which).*movie.*(?:star wars|luke|vader)", "star wars"),
    (r"(?:what|which).*movie.*(?:lion.*king|simba)", "the lion king"),
    (r"(?:what|which).*movie.*(?:toy.*story|woody|buzz)", "toy story"),
    (r"(?:what|which).*(?:actor|actress).*played.*(?:iron man|tony stark)", "robert downey jr"),
    (r"(?:what|which).*(?:actor|actress).*played.*(?:harry potter|wizard boy)", "daniel radcliffe"),
    # -- Animals: male/female names --
    (r"what.*(?:male|boy).*horse.*called", "stallion"),
    (r"what.*(?:female|girl).*horse.*called", "mare"),
    (r"what.*(?:male|boy).*cow.*called", "bull"),
    (r"what.*(?:female|girl).*cow.*called", "cow"),
    (r"what.*(?:male|boy).*sheep.*called", "ram"),
    (r"what.*(?:female|girl).*sheep.*called", "ewe"),
    (r"what.*(?:male|boy).*chicken.*called", "rooster"),
    (r"what.*(?:female|girl).*chicken.*called", "hen"),
    (r"what.*(?:male|boy).*pig.*called", "boar"),
    (r"what.*(?:female|girl).*pig.*called", "sow"),
    (r"what.*(?:male|boy).*deer.*called", "buck"),
    (r"what.*(?:female|girl).*deer.*called", "doe"),
    (r"what.*(?:male|boy).*duck.*called", "drake"),
    (r"what.*(?:female|girl).*duck.*called", "duck"),
    (r"what.*(?:male|boy).*dog.*called", "dog"),
    (r"what.*(?:female|girl).*dog.*called", "bitch"),
    (r"what.*(?:male|boy).*cat.*called", "tom"),
    (r"what.*(?:female|girl).*cat.*called", "queen"),
    (r"what.*(?:male|boy).*lion.*called", "lion"),
    (r"what.*(?:female|girl).*lion.*called", "lioness"),
    # -- More collective nouns --
    (r"(?:group|collective).*(?:cattle|cows)", "herd"),
    (r"(?:group|collective).*(?:sheep)", "flock"),
    (r"(?:group|collective).*(?:geese|goose)", "gaggle"),
    (r"(?:group|collective).*(?:wolves|wolf)", "pack"),
    (r"(?:group|collective).*(?:crows|crow)", "murder"),
    (r"(?:group|collective).*(?:ants|ant)", "colony"),
    (r"(?:group|collective).*(?:dolphins|dolphin)", "pod"),
    (r"(?:group|collective).*(?:whales|whale)", "pod"),
    (r"(?:group|collective).*(?:kangaroos|kangaroo)", "mob"),
    (r"(?:group|collective).*(?:owls|owl)", "parliament"),
    (r"(?:group|collective).*(?:ravens|raven)", "unkindness"),
    (r"(?:group|collective).*(?:flamingos|flamingo)", "flamboyance"),
    # -- Math: more concepts --
    (r"what (?:is|are).*product.*(\d+).*(?:and|times|multiplied).*(\d+)", None),
    (r"what.*(\d+).*times.*(\d+)", None),
    (r"what.*(\d+).*divided.*(?:by|into).*(\d+)", None),
    (r"what.*(?:square|cube).*(\d+)", None),
    (r"what.*(?:even|odd).*number", None),
    (r"what.*(?:prime).*number.*(\d+)", None),
    (r"what.*(?:sum|add).*(\d+).*(?:and|plus).*(\d+)", None),
    (r"what.*(?:difference|subtract).*(\d+).*(?:and|minus|from).*(\d+)", None),
    (r"how many (?:degrees|angular).*(?:triangle)", "180"),
    (r"how many (?:degrees|angular).*(?:square|rectangle|quadrilateral)", "360"),
    (r"how many (?:degrees|angular).*(?:circle)", "360"),
    (r"how many (?:degrees|angular).*(?:right angle)", "90"),
    # -- Roman numerals extended --
    (r"what.*roman numeral.*(?:1|one)$", "i"),
    (r"what.*roman numeral.*(?:2|two)$", "ii"),
    (r"what.*roman numeral.*(?:3|three)$", "iii"),
    (r"what.*roman numeral.*(?:4|four)$", "iv"),
    (r"what.*roman numeral.*(?:5|five)", "v"),
    (r"what.*roman numeral.*(?:6|six)$", "vi"),
    (r"what.*roman numeral.*(?:9|nine)$", "ix"),
    (r"what.*roman numeral.*(?:10|ten)", "x"),
    (r"what.*roman numeral.*(?:50|fifty)", "l"),
    (r"what.*roman numeral.*(?:100|hundred)", "c"),
    (r"what.*roman numeral.*(?:500|five hundred)", "d"),
    (r"what.*roman numeral.*(?:1000|thousand)", "m"),
    # -- More science: elements --
    (r"what.*(?:element|symbol).*(?:gold|\bAu\b)", "au"),
    (r"what.*(?:element|symbol).*(?:silver|\bAg\b)", "ag"),
    (r"what.*(?:element|symbol).*(?:iron|\bFe\b)", "fe"),
    (r"what.*(?:element|symbol).*(?:oxygen|\bO\b)", "o"),
    (r"what.*(?:element|symbol).*(?:carbon|\bC\b)", "c"),
    (r"what.*(?:element|symbol).*(?:hydrogen|\bH\b)", "h"),
    (r"what.*(?:element|symbol).*(?:helium|\bHe\b)", "he"),
    (r"what.*(?:element|symbol).*(?:sodium|\bNa\b)", "na"),
    (r"what.*(?:element|symbol).*(?:potassium|\bK\b)", "k"),
    (r"what.*(?:element|symbol).*(?:calcium|\bCa\b)", "ca"),
    (r"what.*(?:element|symbol).*(?:lead|\bPb\b)", "pb"),
    (r"what.*(?:element|symbol).*(?:mercury|\bHg\b)", "hg"),
    (r"what.*(?:element|symbol).*(?:copper|\bCu\b)", "cu"),
    (r"what.*(?:element|symbol).*(?:zinc|\bZn\b)", "zn"),
    (r"what.*(?:element|symbol).*(?:tin|\bSn\b)", "sn"),
    (r"what.*(?:element|symbol).*(?:aluminum|\bAl\b)", "al"),
    # -- Cooking measurements --
    (r"how many teaspoons.*tablespoon", "3"),
    (r"how many tablespoons.*cup", "16"),
    (r"how many cups.*pint", "2"),
    (r"how many pints.*quart", "2"),
    (r"how many quarts.*gallon", "4"),
    (r"how many ounces.*cup", "8"),
    (r"how many ounces.*pound", "16"),
    # -- Famous people extended --
    (r"(?:who|which).*invented.*(?:airplane|flight|wright)", "wright brothers"),
    (r"(?:who|which).*invented.*(?:radio|wireless)", "marconi"),
    (r"(?:who|which).*invented.*(?:steam engine|locomotive)", "watt"),
    (r"(?:who|which).*invented.*(?:dynamite|explosive)", "nobel"),
    (r"(?:who|which).*invented.*(?:penicillin|antibiotic)", "fleming"),
    (r"(?:who|which).*invented.*(?:telephone)", "bell"),
    (r"(?:who|which).*discovered.*(?:penicillin)", "fleming"),
    (r"(?:who|which).*discovered.*(?:radioactivity|radium)", "marie curie"),
    (r"(?:who|which).*discovered.*(?:dna|double helix)", "watson and crick"),
    (r"(?:who|which).*painted.*(?:sistine chapel|creation of adam)", "michelangelo"),
    (r"(?:who|which).*painted.*(?:starry night|van gogh)", "van gogh"),
    (r"(?:who|which).*sculpted.*(?:david|pieta)", "michelangelo"),
    (r"(?:who|which).*composed.*(?:fifth symphony|fate symphony)", "beethoven"),
    (r"(?:who|which).*composed.*(?:magic flute|requiem|mozart)", "mozart"),
    (r"(?:who|which).*wrote.*(?:theory of relativity|e=mc)", "einstein"),
    (r"(?:who|which).*(?:theory|evolution|natural selection|origin of species)", "darwin"),
    (r"(?:who|which).*(?:civil rights|i have a dream|mlk)", "martin luther king"),
    (r"(?:who|which).*(?:indian independence|nonviolence|gandhi)", "gandhi"),
    # -- Holidays extended --
    (r"what holiday.*(?:december 25|december twenty.fifth|christmas)", "christmas"),
    (r"what holiday.*(?:october 31|october thirty.first)", "halloween"),
    (r"what holiday.*(?:february 14|february fourteenth)", "valentines day"),
    (r"what holiday.*(?:january 1|january first|new year)", "new years day"),
    (r"what holiday.*(?:july 4|july fourth|independence)", "independence day"),
    (r"what holiday.*(?:november.*fourth.*thursday|thanksgiving)", "thanksgiving"),
    (r"what holiday.*(?:march 17|march seventeenth|st patrick)", "st patricks day"),
    (r"what holiday.*(?:may.*fourth|star wars)", "may the fourth"),
    # -- More geography --
    (r"what (?:ocean|sea).*(?:large|biggest|largest)", "pacific"),
    (r"what (?:ocean|sea).*(?:deepest)", "pacific"),
    (r"what (?:ocean|sea).*(?:smallest)", "arctic"),
    (r"what (?:ocean|sea).*(?:coldest)", "arctic"),
    (r"what (?:river|waterway).*longest", "nile"),
    (r"what (?:river|waterway).*(?:largest.*volume|amazon)", "amazon"),
    (r"what (?:mountain|peak).*highest", "everest"),
    (r"what (?:mountain|peak).*tallest.*(?:solar system|mars)", "olympus mons"),
    (r"what (?:continent|landmass).*(?:large|biggest|largest)", "asia"),
    (r"what (?:continent|landmass).*(?:small|littlest|smallest)", "australia"),
    (r"what (?:desert|dry).*largest", "sahara"),
    (r"what (?:desert|dry).*coldest", "antarctica"),
    (r"what (?:island|isle).*(?:largest|biggest)", "greenland"),
    (r"what (?:island|isle).*most.*(?:populous|people)", "java"),
    (r"what (?:lake|lake).*largest.*freshwater", "lake superior"),
    (r"what (?:lake|lake).*deepest", "lake baikal"),
    (r"(?:what|which).*(?:tallest).*(?:waterfall|cascade)|what (?:waterfall|cascade).*tallest", "angel falls"),
    (r"what (?:waterfall|cascade).*largest.*volume", "victoria falls"),
    (r"what (?:country|nation).*largest.*area", "russia"),
    (r"what (?:country|nation).*most.*(?:populous|people|population)", "india"),
    (r"what (?:country|nation).*smallest", "vatican city"),
    (r"what (?:city|metropolis).*most.*(?:populous|people|populated)", "tokyo"),
    # -- Fun / random facts --
    (r"(?:what|which).*(?:fastest|quickest).*speed", "light"),
    (r"speed of light.*(?:m/s|meters per second)", "300000000"),
    (r"speed of sound.*(?:m/s|meters per second)", "343"),
    (r"(?:what|which).*(?:heaviest|densest).*(?:planet|solar)", "jupiter"),
    (r"(?:what|which).*(?:lightest|least dense).*(?:planet|solar)", "saturn"),
    (r"(?:what|which).*(?:closest|nearest).*(?:star|sun)", "sun"),
    (r"(?:what|which).*(?:closest|nearest).*(?:moon|lunar)", "moon"),
    (r"(?:what|which).*(?:farthest|furthest).*(?:planet|solar)", "neptune"),
    (r"(?:what|which).*(?:hottest|warmest).*(?:planet|solar)", "venus"),
    (r"(?:what|which).*(?:coldest|chilliest).*(?:planet|solar)", "neptune"),
    (r"(?:what|which).*(?:planet|solar).*most.*(?:moon|satellite)", "saturn"),
    # ──────────────────────────────────────────────────
    # MASSIVE EXPANSION #2: 400+ more patterns
    # ──────────────────────────────────────────────────
    # -- Yes/No / True/False common questions --
    (r"do cans have labels", "yes"),
    (r"do bottles have caps", "yes"),
    (r"do books have pages", "yes"),
    (r"do cars have engines", "yes"),
    (r"do birds have wings", "yes"),
    (r"do fish have gills", "yes"),
    (r"do dogs have tails", "yes"),
    (r"do cats have whiskers", "yes"),
    (r"do trees have leaves", "yes"),
    (r"do clocks have hands", "yes"),
    (r"is the sky blue", "yes"),
    (r"is water wet", "yes"),
    (r"is fire hot", "yes"),
    (r"is ice cold", "yes"),
    (r"is the earth round", "yes"),
    (r"can birds fly", "yes"),
    (r"can fish swim", "yes"),
    (r"does the sun rise in the east", "yes"),
    (r"does the sun set in the west", "yes"),
    (r"are humans mammals", "yes"),
    (r"are whales mammals", "yes"),
    (r"is the moon (?:a|made).*planet", "no"),
    (r"is the earth flat", "no"),
    (r"can pigs fly", "no"),
    (r"can elephants jump", "no"),
    (r"do snakes have legs", "no"),
    (r"are spiders insects", "no"),
    # -- What is X called / What do you call X --
    (r"what.*call.*(?:baby|young|infant).*dog", "puppy"),
    (r"what.*call.*(?:baby|young|infant).*cat", "kitten"),
    (r"what.*call.*(?:baby|young|infant).*cow", "calf"),
    (r"what.*call.*(?:baby|young|infant).*horse", "foal"),
    (r"what.*call.*(?:baby|young|infant).*sheep", "lamb"),
    (r"what.*call.*(?:baby|young|infant).*pig", "piglet"),
    (r"what.*call.*(?:baby|young|infant).*duck", "duckling"),
    (r"what.*call.*(?:baby|young|infant).*chicken", "chick"),
    (r"what.*call.*(?:baby|young|infant).*goat", "kid"),
    (r"what.*call.*(?:baby|young|infant).*bear", "cub"),
    (r"what.*call.*(?:baby|young|infant).*lion", "cub"),
    (r"what.*call.*(?:baby|young|infant).*tiger", "cub"),
    (r"what.*call.*(?:baby|young|infant).*deer", "fawn"),
    (r"what.*call.*(?:baby|young|infant).*frog", "tadpole"),
    (r"what.*call.*(?:baby|young|infant).*kangaroo", "joey"),
    (r"what.*call.*(?:baby|young|infant).*rabbit", "kit"),
    (r"what.*call.*(?:baby|young|infant).*fox", "kit"),
    (r"what.*call.*(?:baby|young|infant).*eagle", "eaglet"),
    (r"what.*call.*(?:baby|young|infant).*owl", "owlet"),
    (r"what.*call.*(?:baby|young|infant).*swan", "cygnet"),
    (r"what.*call.*(?:baby|young|infant).*(?:whale|dolphin|seal)", "calf"),
    # -- What is the sound of X --
    (r"what.*sound.*(?:dog|dogs|puppy)", "bark"),
    (r"what.*sound.*(?:cat|cats|kitten)", "meow"),
    (r"what.*sound.*(?:cow|cows|cattle)", "moo"),
    (r"what.*sound.*(?:pig|pigs)", "oink"),
    (r"what.*sound.*(?:sheep)", "baa"),
    (r"what.*sound.*(?:horse|horses)", "neigh"),
    (r"what.*sound.*(?:duck|ducks)", "quack"),
    (r"what.*sound.*(?:bird|birds)", "chirp"),
    (r"what.*sound.*(?:rooster|cock)", "crow"),
    (r"what.*sound.*(?:lion|lions)", "roar"),
    (r"what.*sound.*(?:wolf|wolves)", "howl"),
    (r"what.*sound.*(?:frog|frogs)", "ribbit"),
    (r"what.*sound.*(?:snake|snakes)", "hiss"),
    (r"what.*sound.*(?:bee|bees)", "buzz"),
    (r"what.*sound.*(?:owl|owls)", "hoot"),
    (r"what.*sound.*(?:turkey|turkeys)", "gobble"),
    (r"what.*sound.*(?:chicken|chickens|hen)", "cluck"),
    (r"what.*sound.*(?:mouse|mice)", "squeak"),
    (r"what.*sound.*(?:elephant|elephants)", "trumpet"),
    (r"what.*sound.*(?:donkey|donkeys)", "bray"),
    (r"what.*sound.*(?:crow|crows)", "caw"),
    (r"what.*sound.*(?:cricket|crickets)", "chirp"),
    (r"what.*sound.*(?:dolphin|dolphins)", "click"),
    (r"what.*sound.*(?:whale|whales)", "sing"),
    # -- Fill in the blank: "The capital of France is ___" --
    (r"the capital of (?:france|french) is", "paris"),
    (r"the capital of (?:england|britain|uk) is", "london"),
    (r"the capital of (?:spain|spanish) is", "madrid"),
    (r"the capital of (?:italy|italian) is", "rome"),
    (r"the capital of (?:japan|japanese) is", "tokyo"),
    (r"the capital of (?:germany|german) is", "berlin"),
    (r"the capital of (?:china|chinese) is", "beijing"),
    (r"the capital of (?:usa|america|united states) is", "washington dc"),
    (r"the capital of (?:canada|canadian) is", "ottawa"),
    (r"the capital of (?:australia|australian) is", "canberra"),
    (r"the capital of (?:brazil|brazilian) is", "brasilia"),
    (r"the largest (?:ocean|sea) is the", "pacific"),
    (r"the largest (?:continent|landmass) is", "asia"),
    (r"the longest river is the", "nile"),
    (r"the highest mountain is", "everest"),
    (r"the (?:color|colour) of the sky is", "blue"),
    (r"the (?:color|colour) of grass is", "green"),
    (r"the (?:color|colour) of snow is", "white"),
    (r"the (?:color|colour) of blood is", "red"),
    (r"ice is.*(?:frozen|solid)", "water"),
    (r"the sun is a", "star"),
    (r"water boils at.*celsius", "100"),
    (r"water freezes at.*celsius", "0"),
    # -- How many X in Y --
    (r"how many (?:teeth|tooth).*adult human", "32"),
    (r"how many (?:ribs|rib).*human", "24"),
    (r"how many (?:vertebrae|vertebra).*human", "33"),
    (r"how many (?:states|state).*usa|united states", "50"),
    (r"how many (?:provinces|province).*canada", "10"),
    (r"how many (?:countries|country).*europe", "44"),
    (r"how many (?:countries|country).*africa", "54"),
    (r"how many (?:countries|country).*asia", "48"),
    (r"how many (?:countries|country).*south america", "12"),
    (r"how many (?:countries|country).*world", "195"),
    (r"how many (?:stars|star).*american flag", "50"),
    (r"how many (?:stripes|stripe).*american flag", "13"),
    (r"how many (?:elements|element).*periodic table", "118"),
    (r"how many (?:books|book).*bible", "66"),
    (r"how many (?:commandments|commandment)", "10"),
    (r"how many (?:muses|muse).*greek", "9"),
    (r"how many (?:wonders|wonder).*world", "7"),
    (r"how many (?:dwarfs|dwarf).*snow white", "7"),
    (r"how many (?:lives|life).*cat", "9"),
    # -- What is X made of / composed of --
    (r"what.*(?:made|composed|consists).*plastic", "oil"),
    (r"what.*(?:made|composed|consists).*glass", "sand"),
    (r"what.*(?:made|composed|consists).*paper", "wood"),
    (r"what.*(?:made|composed|consists).*steel", "iron"),
    (r"what.*(?:made|composed|consists).*concrete", "cement"),
    (r"what.*(?:made|composed|consists).*cheese", "milk"),
    (r"what.*(?:made|composed|consists).*butter", "cream"),
    (r"what.*(?:made|composed|consists).*yogurt", "milk"),
    (r"what.*(?:made|composed|consists).*tofu", "soybeans"),
    (r"what.*(?:made|composed|consists).*wine", "grapes"),
    (r"what.*(?:made|composed|consists).*vodka", "potatoes"),
    (r"what.*(?:made|composed|consists).*whiskey", "grain"),
    (r"what.*(?:made|composed|consists).*rum", "sugarcane"),
    (r"what.*(?:made|composed|consists).*tequila", "agave"),
    (r"what.*(?:made|composed|consists).*sake", "rice"),
    (r"what.*(?:made|composed|consists).*honey", "bees"),
    (r"what.*(?:made|composed|consists).*silk", "silkworms"),
    (r"what.*(?:made|composed|consists).*pearl", "oysters"),
    (r"what.*(?:made|composed|consists).*rubber", "rubber trees"),
    (r"what.*(?:made|composed|consists).*cotton", "cotton plants"),
    (r"what.*(?:made|composed|consists).*leather", "animal skin"),
    (r"what.*(?:made|composed|consists).*wool", "sheep"),
    # -- Languages spoken in countries --
    (r"what language.*(?:france|french|paris)", "french"),
    (r"what language.*(?:spain|spanish|madrid)", "spanish"),
    (r"what language.*(?:germany|german|berlin)", "german"),
    (r"what language.*(?:italy|italian|rome)", "italian"),
    (r"what language.*(?:japan|japanese|tokyo)", "japanese"),
    (r"what language.*(?:china|chinese|beijing)", "chinese"),
    (r"what language.*(?:russia|russian|moscow)", "russian"),
    (r"what language.*(?:brazil|brazilian)", "portuguese"),
    (r"what language.*(?:portugal|portuguese)", "portuguese"),
    (r"what language.*(?:netherlands|dutch|holland)", "dutch"),
    (r"what language.*(?:greece|greek|athens)", "greek"),
    (r"what language.*(?:turkey|turkish|ankara)", "turkish"),
    (r"what language.*(?:poland|polish|warsaw)", "polish"),
    (r"what language.*(?:sweden|swedish|stockholm)", "swedish"),
    (r"what language.*(?:norway|norwegian|oslo)", "norwegian"),
    (r"what language.*(?:denmark|danish|copenhagen)", "danish"),
    (r"what language.*(?:finland|finnish|helsinki)", "finnish"),
    # -- Currencies --
    (r"what (?:currency|money).*(?:france|french|paris|euro)", "euro"),
    (r"what (?:currency|money).*(?:spain|spanish|euro)", "euro"),
    (r"what (?:currency|money).*(?:germany|german|euro)", "euro"),
    (r"what (?:currency|money).*(?:italy|italian|euro)", "euro"),
    (r"what (?:currency|money).*(?:japan|japanese|yen)", "yen"),
    (r"what (?:currency|money).*(?:china|chinese|yuan|renminbi)", "yuan"),
    (r"what (?:currency|money).*(?:russia|russian|ruble)", "ruble"),
    (r"what (?:currency|money).*(?:uk|britain|british|england|pound)", "pound"),
    (r"what (?:currency|money).*(?:usa|america|united states|dollar)", "dollar"),
    (r"what (?:currency|money).*(?:canada|canadian)", "canadian dollar"),
    (r"what (?:currency|money).*(?:australia|australian)", "australian dollar"),
    (r"what (?:currency|money).*(?:mexico|mexican|peso)", "peso"),
    (r"what (?:currency|money).*(?:brazil|brazilian|real)", "real"),
    (r"what (?:currency|money).*(?:india|indian|rupee)", "rupee"),
    (r"what (?:currency|money).*(?:switzerland|swiss|franc)", "franc"),
    # -- Parts of speech --
    (r"what part of speech.*(?:run|jump|walk|eat|sleep|talk)", "verb"),
    (r"what part of speech.*(?:quickly|slowly|fast|well|badly)", "adverb"),
    (r"what part of speech.*(?:beautiful|ugly|tall|short|big|small)", "adjective"),
    (r"what part of speech.*(?:table|chair|dog|cat|house|car)", "noun"),
    # -- Time zones --
    (r"what.*time zone.*(?:new york|nyc|eastern)", "est"),
    (r"what.*time zone.*(?:los angeles|la|pacific)", "pst"),
    (r"what.*time zone.*(?:london|uk|britain)", "gmt"),
    (r"what.*time zone.*(?:tokyo|japan)", "jst"),
    # -- What comes next in sequence --
    (r"what comes next.*(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)", None),
    (r"what comes next.*(?:january|february|march|april|may|june|july|august|september|october|november|december)", None),
    (r"what comes next.*(?:red|orange|yellow|green|blue|indigo|violet)", None),
    (r"what comes next.*(?:spring|summer|autumn|fall|winter)", None),
    # -- How many letters in alphabet --
    (r"how many letters.*english alphabet", "26"),
    (r"how many vowels.*english", "5"),
    (r"how many consonants.*english", "21"),
    # -- What is the Xth month --
    (r"what.*(?:1st|first) month", "january"),
    (r"what.*(?:2nd|second) month", "february"),
    (r"what.*(?:3rd|third) month", "march"),
    (r"what.*(?:4th|fourth) month", "april"),
    (r"what.*(?:5th|fifth) month", "may"),
    (r"what.*(?:6th|sixth) month", "june"),
    (r"what.*(?:7th|seventh) month", "july"),
    (r"what.*(?:8th|eighth) month", "august"),
    (r"what.*(?:9th|ninth) month", "september"),
    (r"what.*(?:10th|tenth) month", "october"),
    (r"what.*(?:11th|eleventh) month", "november"),
    (r"what.*(?:12th|twelfth) month", "december"),
    # -- What is the Xth day --
    (r"what.*(?:1st|first) day.*week", "sunday"),
    (r"what.*(?:2nd|second) day.*week", "monday"),
    (r"what.*(?:3rd|third) day.*week", "tuesday"),
    (r"what.*(?:4th|fourth) day.*week", "wednesday"),
    (r"what.*(?:5th|fifth) day.*week", "thursday"),
    (r"what.*(?:6th|sixth) day.*week", "friday"),
    (r"what.*(?:7th|seventh) day.*week", "saturday"),
    # -- What is the Xth planet --
    (r"what.*(?:1st|first) planet", "mercury"),
    (r"what.*(?:2nd|second) planet", "venus"),
    (r"what.*(?:3rd|third) planet", "earth"),
    (r"what.*(?:4th|fourth) planet", "mars"),
    (r"what.*(?:5th|fifth) planet", "jupiter"),
    (r"what.*(?:6th|sixth) planet", "saturn"),
    (r"what.*(?:7th|seventh) planet", "uranus"),
    (r"what.*(?:8th|eighth) planet", "neptune"),
    # -- What color is X --
    (r"what color.*(?:apple|strawberry)", "red"),
    (r"what color.*(?:orange fruit|orange citrus)", "orange"),
    (r"what color.*banana", "yellow"),
    (r"what color.*(?:broccoli|cucumber|lettuce)", "green"),
    (r"what color.*(?:blueberry|blue berry)", "blue"),
    (r"what color.*(?:grape|eggplant|plum)", "purple"),
    (r"what color.*(?:lemon|lime)", "yellow"),
    (r"what color.*(?:carrot|pumpkin)", "orange"),
    (r"what color.*(?:tomato|cherry)", "red"),
    (r"what color.*(?:milk|coconut)", "white"),
    (r"what color.*(?:chocolate|coffee)", "brown"),
    (r"what color.*(?:crow|raven|coal)", "black"),
    (r"what color.*(?:flamingo|pig)", "pink"),
    (r"what color.*(?:elephant)", "gray"),
    # -- Which animal --
    (r"which animal.*(?:man's best friend|best friend.*man)", "dog"),
    (r"which animal.*(?:king.*jungle|king.*beasts)", "lion"),
    (r"which animal.*(?:king.*forest)", "tiger"),
    (r"which animal.*(?:stripes|black.*white.*stripes)", "zebra"),
    (r"which animal.*(?:spots.*fast|fastest.*land)", "cheetah"),
    (r"which animal.*(?:long neck|tallest)", "giraffe"),
    (r"which animal.*(?:trunk|largest.*land)", "elephant"),
    (r"which animal.*(?:pouch|jumps.*australia)", "kangaroo"),
    (r"which animal.*(?:bamboo|black.*white.*china)", "panda"),
    (r"which animal.*(?:honey|hibernate|cave)", "bear"),
    (r"which animal.*(?:howls.*moon|pack)", "wolf"),
    (r"which animal.*(?:eight.*legs|web)", "spider"),
    (r"which animal.*(?:no.*legs|slithers)", "snake"),
    (r"which animal.*(?:can.*change.*color|camouflage)", "chameleon"),
    (r"which animal.*(?:rolls.*ball|spike)", "hedgehog"),
    (r"which animal.*(?:glows|light.*bug)", "firefly"),
    (r"which animal.*(?:plays dead|pretends)", "opossum"),
    (r"which animal.*(?:builds.*dam|flat tail)", "beaver"),
    (r"which animal.*(?:echolocation|sonar|blind.*bat)", "bat"),
    (r"which animal.*(?:migration|v.*shape|fly.*south)", "goose"),
    (r"which (?:bird|animal).*(?:talk|mimic|speak|parrot)", "parrot"),
    (r"which (?:bird|animal).*(?:largest.*egg|ostrich)", "ostrich"),
    (r"which (?:bird|animal).*(?:smallest|humming)", "bee hummingbird"),
    # -- NFL / NBA / sports teams --
    (r"how many (?:players|people).*(?:nfl|football).*(?:field|team)", "11"),
    (r"how many (?:players|people).*(?:nba|basketball).*(?:court|team)", "5"),
    (r"how many (?:players|people).*(?:mlb|baseball).*(?:field|team)", "9"),
    (r"how many (?:players|people).*(?:nhl|hockey|ice).*(?:ice|team)", "6"),
    (r"how many (?:points|point).*(?:touchdown|td)", "6"),
    (r"how many (?:points|point).*(?:field goal|fg).*football", "3"),
    (r"how many (?:points|point).*(?:basket|basketball)", "2"),
    (r"how many (?:points|point).*(?:three pointer|three.pt)", "3"),
    (r"how many (?:points|point).*(?:free throw)", "1"),
    (r"how many (?:points|point).*(?:soccer goal|football goal)", "1"),
    (r"how many (?:innings|inning).*(?:baseball|mlb)", "9"),
    (r"how many (?:quarters|quarter).*(?:basketball|nba)", "4"),
    (r"how many (?:quarters|quarter).*(?:football|nfl)", "4"),
    (r"how many (?:periods|period).*(?:hockey|nhl)", "3"),
    (r"how many (?:halves|half).*(?:soccer|football)", "2"),
    # -- Movies & TV --
    (r"(?:what|which).*(?:longest|most).*running.*tv show", "the simpsons"),
    (r"(?:what|which).*(?:highest|most).*grossing.*movie", "avatar"),
    (r"(?:what|which).*first.*animated.*feature.*film", "snow white"),
    (r"(?:who|which).*(?:character|hero).*(?:spider.man|spiderman)", "peter parker"),
    (r"(?:who|which).*(?:character|hero).*(?:batman|bruce)", "bruce wayne"),
    (r"(?:who|which).*(?:character|hero).*(?:superman|clark)", "clark kent"),
    # -- Internet / tech --
    (r"what.*(?:www|world wide web)", "world wide web"),
    (r"what.*(?:html|hypertext)", "hypertext markup language"),
    (r"what.*(?:css|cascading)", "cascading style sheets"),
    (r"what.*(?:http|hypertext transfer)", "hypertext transfer protocol"),
    (r"what.*(?:url|uniform resource)", "uniform resource locator"),
    (r"what.*(?:dns|domain name)", "domain name system"),
    (r"what.*(?:isp|internet service)", "internet service provider"),
    (r"what.*(?:vpn|virtual private)", "virtual private network"),
    (r"what.*(?:artificial\s+intelligence|\bai\b)", "artificial intelligence"),
    (r"(?:what|which).*most.*abundant.*gas.*(?:air|atmosphere)", "nitrogen"),
    (r"(?:what|which).*first.*element.*periodic table", "hydrogen"),
    (r"(?:what|which).*fastest.*fish", "sailfish"),
    (r"(?:what|which).*largest.*moon.*saturn|saturn.*largest.*moon", "titan"),
    (r"(?:what|which).*deepest.*(?:ocean.*)?trench", "mariana trench"),
    (r"how many minutes.*day", "1440"),
    (r"how many seconds.*hour", "3600"),
    (r"(?:what|which).*capital.*new zealand", "wellington"),
    (r"(?:what|which).*currency.*united kingdom", "pound"),
    (r"(?:what|which).*only.*mammal.*(?:flight|fly)", "bat"),
    (r"how many hearts.*octopus", "3"),
    (r"(?:what|which).*fastest.*grow(?:ing)?.*plant", "bamboo"),
    (r"what.*(?:operating system|\bos\b)", "operating system"),
    (r"what.*(?:cpu|central processing)", "central processing unit"),
    (r"what.*(?:ram|random access)", "random access memory"),
    (r"what.*(?:rom|read only)", "read only memory"),
    (r"what.*(?:usb|universal serial)", "universal serial bus"),
    (r"what.*(?:wifi|wi.fi|wireless fidelity)", "wireless fidelity"),
    (r"what.*(?:lte|long term)", "long term evolution"),
    (r"what.*(?:led|light emitting)", "light emitting diode"),
    (r"what.*(?:lcd|liquid crystal)", "liquid crystal display"),
    (r"what.*(?:pdf|portable document)", "portable document format"),
    (r"what.*(?:jpeg|jpg|joint photographic)", "joint photographic experts group"),
    (r"what.*(?:gif|graphics interchange)", "graphics interchange format"),
    (r"what.*(?:png|portable network)", "portable network graphics"),
    # -- More body parts / anatomy --
    (r"what.*(?:strongest|most powerful).*muscle", "tongue"),
    (r"what.*(?:largest|biggest).*organ", "skin"),
    (r"what.*(?:smallest|tiniest).*bone", "stapes"),
    (r"what.*(?:longest|biggest).*bone", "femur"),
    (r"what.*(?:hardest|strongest).*substance.*body", "enamel"),
    (r"what.*(?:largest|biggest).*artery", "aorta"),
    (r"what.*(?:largest|biggest).*vein", "vena cava"),
    (r"what.*(?:fastest|quickest).*healing.*organ", "liver"),
    (r"what.*(?:only).*organ.*(?:regenerate|regrow)", "liver"),
    # -- Famous landmarks --
    (r"(?:what|which).*statue.*(?:liberty|freedom).*new york", "statue of liberty"),
    (r"(?:what|which).*eiffel.*tower.*(?:paris|france)", "eiffel tower"),
    (r"(?:what|which).*great wall.*(?:china)", "great wall of china"),
    (r"(?:what|which).*taj mahal", "taj mahal"),
    (r"(?:what|which).*colosseum|coliseum", "colosseum"),
    (r"(?:what|which).*machu picchu", "machu picchu"),
    (r"(?:what|which).*pyramid.*(?:giza|egypt)", "great pyramid"),
    (r"(?:what|which).*sphinx.*egypt", "sphinx"),
    (r"(?:what|which).*leaning.*tower.*pisa", "leaning tower of pisa"),
    (r"(?:what|which).*golden gate.*bridge", "golden gate bridge"),
    (r"(?:what|which).*big ben", "big ben"),
    (r"(?:what|which).*opera house.*sydney", "sydney opera house"),
    (r"(?:what|which).*stonehenge", "stonehenge"),
    (r"(?:what|which).*mount rushmore", "mount rushmore"),
    (r"(?:what|which).*grand canyon", "grand canyon"),
    (r"(?:what|which).*niagara falls", "niagara falls"),
    # -- Religions extended --
    (r"how many (?:pillars|pillar).*islam", "5"),
    (r"how many (?:noble truths|truth).*buddhism", "4"),
    (r"what.*holy book.*(?:christianity|christian|bible)", "bible"),
    (r"what.*holy book.*(?:islam|muslim|quran)", "quran"),
    (r"what.*holy book.*(?:judaism|jewish|torah)", "torah"),
    (r"what.*holy book.*(?:hinduism|hindu|vedas)", "vedas"),
    # -- Zodiac --
    (r"how many.*zodiac signs", "12"),
    (r"what.*zodiac.*(?:january|jan)", "capricorn"),
    (r"what.*zodiac.*(?:february|feb)", "aquarius"),
    (r"what.*zodiac.*(?:march|mar)", "pisces"),
    (r"what.*zodiac.*(?:april|apr)", "aries"),
    (r"what.*zodiac.*(?:may)", "taurus"),
    (r"what.*zodiac.*(?:june|jun)", "gemini"),
    (r"what.*zodiac.*(?:july|jul)", "cancer"),
    (r"what.*zodiac.*(?:august|aug)", "leo"),
    (r"what.*zodiac.*(?:september|sep)", "virgo"),
    (r"what.*zodiac.*(?:october|oct)", "libra"),
    (r"what.*zodiac.*(?:november|nov)", "scorpio"),
    (r"what.*zodiac.*(?:december|dec)", "sagittarius"),
    # -- Yes/No KB patterns --
    (r"are.*canned.*food.*preserv", "yes"),
    (r"are.*canned.*good.*preserv", "yes"),
    (r"is.*canned.*food.*safe", "yes"),
    (r"are.*pickled.*food.*preserv", "yes"),
    (r"are.*dried.*food.*preserv", "yes"),
    (r"are.*frozen.*(?:food|vegetable|fruit).*preserv", "yes"),
    (r"do.*magnets.*attract.*(?:iron|metal|steel)", "yes"),
    (r"do.*magnets.*attract.*(?:plastic|wood|paper|glass)", "no"),
    (r"is.*glass.*breakable", "yes"),
    (r"is.*(?:bread|pizza|cake|pie).*baked.*oven", "yes"),
    (r"is.*metal.*conductive", "yes"),
    (r"is.*wood.*conductive", "no"),
    (r"is.*rubber.*(?:conductive|conductor)", "no"),
    (r"do.*plants.*need.*sunlight", "yes"),
    (r"do.*plants.*need.*water", "yes"),
    (r"do.*humans.*need.*(?:sleep|food|water|oxygen|exercise)", "yes"),
    (r"is.*exercise.*good.*(?:health|you)", "yes"),
    (r"is.*smoking.*(?:bad|harmful|dangerous)", "yes"),
    (r"are.*lions.*carnivor", "yes"),
    (r"are.*sharks.*(?:dangerous|predator)", "yes"),
    (r"are.*all.*sharks.*dangerous", "no"),
    (r"are.*whales.*(?:largest|biggest).*animal", "yes"),
    (r"do.*bees.*(?:make|produce).*honey", "yes"),
    (r"do.*spiders.*(?:make|spin).*web", "yes"),
    (r"are.*frogs.*amphibian", "yes"),
    (r"are.*snakes.*reptile", "yes"),
    (r"are.*parrots.*can.*talk", "yes"),
    (r"do.*penguins.*live.*(?:cold|antarctica|south)", "yes"),
    (r"do.*camels.*live.*desert", "yes"),
    (r"are.*giraffes.*(?:tallest|tall)", "yes"),
    (r"are.*cheetahs.*(?:fastest|fast)", "yes"),
    (r"are.*sloths.*slow", "yes"),
    (r"does.*heart.*pump.*blood", "yes"),
    (r"do.*lungs.*(?:breath|oxygen|exchange)", "yes"),
    (r"is.*skin.*(?:largest|biggest).*organ", "yes"),
    (r"is.*blood.*red", "yes"),
    (r"does.*gravity.*(?:exist|pull)", "yes"),
    (r"does.*light.*travel.*faster.*sound", "yes"),
    (r"does.*sound.*travel.*(?:through|in).*(?:air|water)", "yes"),
    (r"does.*sound.*travel.*(?:through|in).*vacuum", "no"),
    (r"does.*water.*expand.*(?:when|if).*frozen", "yes"),
    (r"is.*ice.*(?:less|more).*dense.*water", "yes"),
    (r"does.*salt.*dissolve.*water", "yes"),
    (r"does.*sugar.*dissolve.*water", "yes"),
    (r"does.*sand.*dissolve.*water", "no"),
    (r"does.*oil.*(?:dissolve|mix).*water", "no"),
    (r"does.*iron.*rust", "yes"),
    (r"does.*gold.*rust", "no"),
    (r"are.*diamonds.*(?:hard|hardest)", "yes"),
    (r"is.*hydrogen.*lightest.*element", "yes"),
    (r"is.*jupiter.*largest.*planet", "yes"),
    (r"does.*saturn.*have.*ring", "yes"),
    (r"is.*mars.*red.*planet", "yes"),
    (r"is.*venus.*(?:hottest|hot).*planet", "yes"),
    (r"is.*mercury.*closest.*sun", "yes"),
    (r"is.*the.*universe.*expanding", "yes"),
    (r"do.*black.*holes.*exist", "yes"),
    (r"is.*antarctica.*(?:cold|continent)", "yes"),
    (r"is.*everest.*highest.*mountain", "yes"),
    (r"is.*pacific.*largest.*ocean", "yes"),
    (r"is.*russia.*largest.*country", "yes"),
    (r"do.*computers.*(?:use|need).*electricity", "yes"),
    (r"is.*wifi.*wireless", "yes"),
    (r"is.*bluetooth.*wireless", "yes"),
    (r"do.*gps.*use.*satellite", "yes"),
    (r"do.*satellites.*orbit.*earth", "yes"),
    (r"can.*viruses.*infect.*computer", "yes"),
    (r"do.*schools.*educate", "yes"),
    (r"do.*libraries.*(?:have|lend).*book", "yes"),
    (r"do.*hospitals.*treat.*patient", "yes"),
    (r"do.*firefighters.*put.*out.*fire", "yes"),
    (r"do.*police.*enforce.*law", "yes"),
    (r"is.*bread.*made.*(?:of|from).*flour", "yes"),
    (r"is.*cheese.*made.*(?:of|from).*milk", "yes"),
    (r"is.*wine.*made.*(?:of|from).*grape", "yes"),
    (r"is.*chocolate.*made.*(?:of|from).*cocoa", "yes"),
    (r"is.*butter.*made.*(?:of|from).*cream", "yes"),
    (r"are.*tomatoes.*fruit", "yes"),
    (r"are.*strawberries.*fruit", "yes"),
    (r"are.*bananas.*fruit", "yes"),
    (r"are.*oranges.*citrus", "yes"),
    (r"are.*mushrooms.*fungus", "yes"),
    (r"can.*vaccines.*prevent.*disease", "yes"),
    (r"do.*antibiotics.*kill.*bacteria", "yes"),
    (r"do.*antibiotics.*kill.*virus", "no"),
    (r"are.*bacteria.*microscopic", "yes"),
    (r"are.*viruses.*microscopic", "yes"),
    # -- Quick true/false catch --
    (r"(?:true|false).*canned.*food.*preserv", "true"),
    (r"(?:true|false).*sun.*(?:hot|star)", "true"),
    (r"(?:true|false).*water.*wet", "true"),
    (r"(?:true|false).*fire.*(?:cold|not hot)", "false"),
    (r"(?:true|false).*earth.*(?:flat|center).*(?:universe|solar)", "false"),
    (r"(?:true|false).*earth.*(?:round|orbit.*sun)", "true"),
    (r"(?:true|false).*human.*(?:have.*tail|can.*fly|breath.*underwater)", "false"),
    (r"(?:true|false).*human.*(?:have.*two.*eye|need.*water|need.*oxygen)", "true"),
    (r"(?:true|false).*bird.*mammal", "false"),
    (r"(?:true|false).*whale.*mammal", "true"),
    (r"(?:true|false).*spider.*insect", "false"),
    (r"(?:true|false).*frog.*amphibian", "true"),
    (r"(?:true|false).*snake.*reptile", "true"),
    (r"(?:true|false).*mushroom.*plant", "false"),
    (r"(?:true|false).*mushroom.*fungus", "true"),
    (r"(?:true|false).*tomato.*vegetable", "false"),
    (r"(?:true|false).*tomato.*fruit", "true"),
    (r"(?:true|false).*gravity.*exist", "true"),
    (r"(?:true|false).*(?:light.*slower|sound.*faster)", "false"),
    (r"(?:true|false).*light.*faster.*sound", "true"),
    (r"(?:true|false).*sun.*orbit.*earth", "false"),
    (r"(?:true|false).*earth.*orbit.*sun", "true"),
    (r"(?:true|false).*moon.*planet", "false"),
    (r"(?:true|false).*moon.*satellite", "true"),
    (r"(?:true|false).*diamond.*(?:hard|hardest)", "true"),
    (r"(?:true|false).*water.*expand.*frozen", "true"),
    (r"(?:true|false).*ice.*sink.*water", "false"),
    (r"(?:true|false).*ice.*float.*water", "true"),
    (r"(?:true|false).*salt.*dissolve.*water", "true"),
    (r"(?:true|false).*oil.*mix.*water", "false"),
    (r"(?:true|false).*iron.*rust", "true"),
    (r"(?:true|false).*gold.*rust", "false"),
    (r"(?:true|false).*penguin.*fly", "false"),
    (r"(?:true|false).*penguin.*bird", "true"),
    (r"(?:true|false).*bat.*blind", "false"),
    (r"(?:true|false).*bat.*mammal", "true"),
    (r"(?:true|false).*exercise.*(?:bad|harmful)", "false"),
    (r"(?:true|false).*exercise.*(?:good|healthy|beneficial)", "true"),
    (r"(?:true|false).*smoking.*(?:good|healthy|harmless)", "false"),
    (r"(?:true|false).*smoking.*(?:bad|harmful|dangerous)", "true"),
    (r"(?:true|false).*vaccine.*(?:bad|harmful|dangerous).*(?:all|always)", "false"),
    (r"(?:true|false).*vaccine.*prevent.*disease", "true"),
    (r"(?:true|false).*dogs.*can.*fly", "false"),
    (r"(?:true|false).*cats.*can.*(?:bark|fly)", "false"),
    (r"(?:true|false).*fish.*can.*(?:fly|walk)", "false"),
    (r"(?:true|false).*fish.*can.*swim", "true"),
    # -- Phenomenon / process / science --
    (r"what.*phenomenon.*light.*(?:bend|refract).*water", "refraction"),
    (r"what.*phenomenon.*light.*bounce.*surface", "reflection"),
    (r"what.*phenomenon.*sound.*bounce.*surface", "echo"),
    (r"what.*phenomenon.*water.*(?:cycle|evapor|condens)", "water cycle"),
    (r"what.*phenomenon.*earth.*shake", "earthquake"),
    (r"what.*phenomenon.*mountain.*erupt.*lava", "volcano"),
    (r"what.*phenomenon.*wind.*spin.*funnel", "tornado"),
    (r"what.*phenomenon.*ocean.*storm.*spin", "hurricane"),
    (r"what.*phenomenon.*tide.*(?:rise|fall)", "tides"),
    (r"what.*phenomenon.*(?:moon|sun).*(?:block|cover|eclipse)", "eclipse"),
    (r"what.*process.*plant.*make.*food.*sunlight", "photosynthesis"),
    (r"what.*process.*water.*vapor.*boil", "evaporation"),
    (r"what.*process.*gas.*liquid.*cool", "condensation"),
    (r"what.*process.*liquid.*solid.*freeze", "freezing"),
    (r"what.*process.*solid.*liquid.*melt", "melting"),
    (r"what.*process.*(?:ice.*vapor|solid.*gas.*skip)", "sublimation"),
    (r"what.*process.*food.*energy.*cell", "respiration"),
    (r"what.*process.*rock.*break.*weather", "weathering"),
    (r"what.*process.*soil.*wash.*away", "erosion"),
    # -- Phobias --
    (r"what.*fear.*spider", "arachnophobia"),
    (r"what.*fear.*height", "acrophobia"),
    (r"what.*fear.*water", "hydrophobia"),
    (r"what.*fear.*(?:dark|night)", "nyctophobia"),
    (r"what.*fear.*(?:confined|small|enclosed)", "claustrophobia"),
    # -- Who painted/wrote/composed --
    (r"what.*paint.*mona lisa", "leonardo da vinci"),
    (r"what.*paint.*starry.*night", "vincent van gogh"),
    (r"what.*paint.*(?:sistine|creation.*adam)", "michelangelo"),
    (r"what.*compose.*fifth.*symphony", "beethoven"),
    (r"what.*compose.*(?:magic.*flute|requiem|eine.*kleine)", "mozart"),
    (r"what.*compose.*four.*season", "vivaldi"),
    (r"what.*write.*(?:hamlet|macbeth|othello|king.*lear)", "shakespeare"),
    (r"what.*write.*(?:moby.*dick|great.*white.*whale)", "herman melville"),
    (r"what.*write.*(?:1984|animal.*farm|big.*brother)", "george orwell"),
    (r"what.*write.*(?:alice.*wonderland|cheshire|mad.*hatter)", "lewis carroll"),
    (r"what.*write.*(?:pride.*prejudice|jane.*austen)", "jane austen"),
    (r"what.*write.*great.*gatsby", "f. scott fitzgerald"),
    (r"what.*write.*harry.*potter", "j.k. rowling"),
    # -- Years / dates --
    (r"what.*year.*moon.*landing", "1969"),
    (r"what.*year.*(?:wwii|world war two|world war ii).*end", "1945"),
    (r"what.*year.*(?:wwi|world war one|world war i).*end", "1918"),
    (r"what.*year.*titanic.*sink", "1912"),
    (r"what.*year.*berlin.*wall.*fall", "1989"),
    (r"what.*year.*(?:american|usa|us).*independence", "1776"),
    (r"what.*year.*french.*revolution.*start", "1789"),
    (r"what.*year.*columbus.*(?:sail|discover)", "1492"),
    # -- Square roots / cube roots --
    (r"what.*(?:square root|sqrt).*(?:of|for).*9", "3"),
    # -- Sleepwear / clothing for sleep --
    (r"what.*(?:warm )?clothing.*(?:cover|covers).*(?:whole|entire).*body.*(?:for|during|while).*sleep", "pajamas"),
    (r"what.*(?:warm )?clothing.*(?:cover|covers).*body.*(?:for|during|while).*sleep", "pajamas"),
    (r"what.*clothing.*(?:wear|worn).*(?:for|during|at).*sleep", "pajamas"),
    (r"what.*(?:do|does|do you|do we).*(?:wear|put on).*(?:for|during|at).*sleep", "pajamas"),
    (r"what.*(?:wear|worn).*(?:while|when).*sleeping", "pajamas"),
    (r"what.*(?:clothing|clothes|garment|outfit).*sleep", "pajamas"),
    (r"what.*(?:night|bed).*time.*(?:clothing|clothes|wear|garment)", "pajamas"),
    (r"what.*(?:clothing|garment).*(?:whole|entire).*body", "pajamas"),
    (r"what.*pajama.*(?:called|known as)", "pajamas"),
    (r"what.*(?:nightwear|nightgown).*(?:called|known as)", "nightgown"),
    (r"what.*clothing.*women.*sleep.*(?:one piece|dress.*style)", "nightgown"),
    (r"what.*(?:robe|bathrobe).*(?:called|known as)", "bathrobe"),
    (r"what.*clothing.*(?:wrap|cover).*after.*(?:bath|shower)", "bathrobe"),
    (r"what.*slippers.*(?:wear|worn).*(?:feet|foot).*house", "slippers"),
    (r"what.*footwear.*(?:wear|worn).*indoors", "slippers"),

    (r"what.*(?:square root|sqrt).*(?:of|for).*16", "4"),
    (r"what.*(?:square root|sqrt).*(?:of|for).*25", "5"),
    (r"what.*(?:square root|sqrt).*(?:of|for).*36", "6"),
    (r"what.*(?:square root|sqrt).*(?:of|for).*49", "7"),
    (r"what.*(?:square root|sqrt).*(?:of|for).*64", "8"),
    (r"what.*(?:square root|sqrt).*(?:of|for).*81", "9"),
    (r"what.*(?:square root|sqrt).*(?:of|for).*100", "10"),
    (r"what.*(?:square root|sqrt).*(?:of|for).*144", "12"),
    (r"what.*(?:cube root).*(?:of|for).*8", "2"),
    (r"what.*(?:cube root).*(?:of|for).*27", "3"),
    (r"what.*(?:cube root).*(?:of|for).*64", "4"),
    (r"what.*(?:cube root).*(?:of|for).*125", "5"),
    # -- First person to X --
    (r"what.*first.*person.*climb.*everest", "edmund hillary"),
    (r"what.*first.*person.*(?:orbit|in space)", "yuri gagarin"),
    (r"what.*first.*person.*moon", "neil armstrong"),
    (r"what.*first.*woman.*solo.*atlantic", "amelia earhart"),
    (r"what.*first.*president.*united.*state", "george washington"),
    # -- Math facts --
    (r"what.*(?:value|number).*pi.*(?:rounded|nearest|whole|integer)", "3"),
    # -- Presidents --
    (r"what.*president.*(?:civil war|lincoln|abolish.*slavery)", "abraham lincoln"),
    (r"what.*president.*(?:great.*depression|new deal|pearl harbor)", "franklin roosevelt"),
    (r"what.*president.*(?:watergate|resign.*nixon)", "richard nixon"),
    (r"what.*president.*(?:independence|first|founding|constitution)", "george washington"),
    # -- Sciences --
    (r"what.*(?:study|science).*(?:star|planet|space|universe|celestial)", "astronomy"),
    (r"what.*(?:study|science).*(?:weather|climate|atmosphere|forecast)", "meteorology"),
    (r"what.*(?:study|science).*(?:living.*organism|life)", "biology"),
    (r"what.*(?:study|science).*(?:rock|earth.*structure|geology)", "geology"),
    (r"what.*(?:study|science).*(?:ancient|artifact.*dig|archaeology)", "archaeology"),
    (r"what.*(?:study|science).*(?:chemical|element|reaction|molecule)", "chemistry"),
    (r"what.*(?:study|science).*(?:motion|force|energy|quantum)", "physics"),
    (r"what.*(?:study|science).*(?:mind|behavior|psychology)", "psychology"),
    # -- Symbols --
    (r"which.*animal.*symbol.*peace", "dove"),
    (r"which.*animal.*symbol.*wisdom", "owl"),
    (r"which.*animal.*symbol.*(?:courage|brave)", "lion"),
    (r"which.*animal.*symbol.*(?:loyal|loyalty)", "dog"),
    (r"which.*animal.*symbol.*freedom", "eagle"),
    (r"which.*animal.*symbol.*(?:cunning|sly|crafty)", "fox"),
    (r"which.*animal.*symbol.*(?:strength|power)", "lion"),
    (r"which.*animal.*symbol.*love", "dove"),
    # -- Food riddles / body-washing (real hCaptcha questions) --
    (r"what\s+vegetable\s+is\s+white\s+inside\s+(?:and\s+)?brown\s+outside", "potato"),
    (r"what\s+vegetable\s+is\s+brown\s+outside\s+(?:and\s+)?white\s+inside", "potato"),
    (r"what\s+vegetable\s+is\s+white\s+on\s+the\s+inside\s+and\s+brown\s+on\s+the\s+outside", "potato"),
    (r"what\s+vegetable\s+is\s+white\s+inside", "potato"),
    (r"what\s+is\s+white\s+inside\s+and\s+brown\s+outside", "potato"),
    (r"what\s+liquid\s+do\s+you\s+use\s+to\s+wash\s+your\s+body", "water"),
    (r"what\s+liquid\s+(?:do|can|would)\s+you\s+use\s+to\s+(?:wash|clean)\s+your\s+body", "water"),

]
# Category word sets for "which of these is a/an X" pickers
# Category word sets for "which of these is a/an X" pickers
CATEGORY_WORDS = {
    "fruit": frozenset([
        "apple", "apricot", "avocado", "banana", "blackberry", "blueberry",
        "cherry", "coconut", "fig", "grape", "grapefruit", "kiwi", "lemon",
        "lime", "mango", "melon", "nectarine", "orange", "papaya", "peach",
        "pear", "pineapple", "plum", "pomegranate", "raspberry", "strawberry",
        "watermelon", "cantaloupe",
    ]),
    "vegetable": frozenset([
        "asparagus", "beet", "broccoli", "cabbage", "carrot", "cauliflower",
        "celery", "corn", "cucumber", "eggplant", "garlic", "kale", "lettuce",
        "onion", "pea", "pepper", "potato", "pumpkin", "radish", "spinach",
        "squash", "tomato", "turnip", "zucchini",
    ]),
    "color": frozenset([
        "red", "blue", "green", "yellow", "orange", "purple", "pink",
        "brown", "black", "white", "gray", "grey", "cyan", "magenta", "teal",
    ]),
    "room": frozenset([
        "kitchen", "bedroom", "bathroom", "living room", "dining room",
        "garage", "basement", "attic", "hallway", "office", "laundry room",
        "bath room",
    ]),
    "insect": frozenset([
        "ant", "bee", "beetle", "butterfly", "caterpillar", "cockroach",
        "cricket", "dragonfly", "fly", "grasshopper", "ladybug", "mantis",
        "mosquito", "moth", "wasp", "termite",
    ]),
    "bird": frozenset([
        "eagle", "hawk", "owl", "penguin", "parrot", "peacock", "pigeon",
        "robin", "sparrow", "swallow", "swan", "turkey", "duck", "goose",
        "chicken", "raven", "crow", "flamingo", "ostrich", "woodpecker",
    ]),
    "fish": frozenset([
        "salmon", "tuna", "trout", "shark", "goldfish", "cod", "halibut",
        "sardine", "anchovy", "eel", "catfish", "bass",
    ]),
    "clothing": frozenset([
        "shirt", "pants", "dress", "skirt", "jacket", "coat", "sweater",
        "hat", "socks", "shoes", "boots", "gloves", "scarf", "belt", "tie",
    ]),
    "body": frozenset([
        "arm", "leg", "head", "hand", "foot", "eye", "ear", "nose", "mouth",
        "finger", "toe", "knee", "elbow", "shoulder", "hair", "tongue",
    ]),
    "vehicle": frozenset([
        "car", "truck", "bus", "bicycle", "motorcycle", "train", "plane",
        "boat", "ship", "helicopter", "taxi", "van",
    ]),
    "musical instrument": frozenset([
        "guitar", "piano", "drums", "violin", "flute", "trumpet", "saxophone",
        "cello", "harp", "accordion", "clarinet", "trombone",
    ]),
    "season": frozenset(["spring", "summer", "autumn", "fall", "winter"]),
    "month": frozenset([
        "january", "february", "march", "april", "may", "june", "july",
        "august", "september", "october", "november", "december",
    ]),
    "day": frozenset([
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
        "sunday",
    ]),
    "planet": frozenset([
        "mercury", "venus", "earth", "mars", "jupiter", "saturn",
        "uranus", "neptune",
    ]),
    "profession": frozenset([
        "doctor", "teacher", "nurse", "police", "firefighter", "lawyer",
        "engineer", "chef", "pilot", "farmer", "scientist", "artist",
    ]),
    "tool": frozenset([
        "hammer", "screwdriver", "wrench", "saw", "drill", "pliers",
        "axe", "shovel", "rake",
    ]),
    "weather": frozenset([
        "rain", "snow", "sun", "wind", "cloud", "storm", "fog", "hail",
    ]),
}


# ── Semantic answer table: topic keywords -> answer ──
# Matches ANY phrasing that contains the topic keywords, catching
# question variations the regex patterns miss. Checked after patterns.
SEMANTIC_ANSWERS = [
    # (required keywords ALL present, answer)
    (["pet food", "cans"], "dog food"),
    (["dog food", "cans"], "dog food"),
    (["cat food", "cans"], "cat food"),
    (["string instrument", "strings"], "guitar"),
    (["instrument", "six", "strings"], "guitar"),
    (["instrument", "four", "strings"], "violin"),
    (["instrument", "eighty", "keys"], "piano"),
    (["instrument", "black", "white", "keys"], "piano"),
    (["instrument", "keys"], "piano"),
    (["organ", "pumps", "blood"], "heart"),
    (["organ", "breathe"], "lungs"),
    (["organ", "think"], "brain"),
    (["organ", "digest"], "stomach"),
    (["organ", "filter", "blood"], "kidney"),
    (["largest", "organ"], "skin"),
    (["planet", "closest", "sun"], "mercury"),
    (["planet", "rings"], "saturn"),
    (["planet", "red"], "mars"),
    (["planet", "hottest"], "venus"),
    (["planet", "largest"], "jupiter"),
    (["planet", "live"], "earth"),
    (["planet", "rings"], "saturn"),
    (["ocean", "largest"], "pacific"),
    (["ocean", "smallest"], "arctic"),
    (["continent", "largest"], "asia"),
    (["continent", "smallest"], "australia"),
    (["river", "longest"], "nile"),
    (["mountain", "highest"], "everest"),
    (["country", "largest", "area"], "russia"),
    (["desert", "largest"], "sahara"),
    (["continent", "coldest"], "antarctica"),
    (["soccer", "players"], "11"),
    (["basketball", "players"], "5"),
    (["baseball", "players"], "9"),
    (["hockey", "players"], "6"),
    (["golf", "holes"], "18"),
    (["seconds", "minute"], "60"),
    (["minutes", "hour"], "60"),
    (["hours", "day"], "24"),
    (["days", "week"], "7"),
    (["months", "year"], "12"),
    (["planets", "solar"], "8"),
    (["continents"], "7"),
    (["oceans"], "5"),
    (["bones", "body"], "206"),
    (["chambers", "heart"], "4"),
    (["colors", "rainbow"], "7"),
    (["sides", "triangle"], "3"),
    (["sides", "square"], "4"),
    (["sides", "pentagon"], "5"),
    (["sides", "hexagon"], "6"),
    (["sides", "octagon"], "8"),
    (["string", "six"], "guitar"),
    (["grain", "flour"], "wheat"),
    (["grain", "bread"], "wheat"),
    (["flour", "bread"], "wheat"),
    (["animal", "moo"], "cow"),
    (["animal", "barks"], "dog"),
    (["animal", "meows"], "cat"),
    (["animal", "quacks"], "duck"),
    (["animal", "oinks"], "pig"),
    (["animal", "neighs"], "horse"),
    (["animal", "baa"], "sheep"),
    (["animal", "roars"], "lion"),
    (["animal", "howls"], "wolf"),
    (["animal", "chirps"], "bird"),
    (["animal", "ribbit"], "frog"),
    (["animal", "hisses"], "snake"),
    (["animal", "hoots"], "owl"),
    (["animal", "gobbles"], "turkey"),
    (["animal", "buzzes"], "bee"),
    (["animal", "clucks"], "chicken"),
    (["bees", "make"], "honey"),
    (["chickens", "lay"], "eggs"),
    (["cow", "produce"], "milk"),
    (["frozen", "water"], "ice"),
    (["opposite", "up"], "down"),
    (["opposite", "hot"], "cold"),
    (["opposite", "day"], "night"),
    (["opposite", "left"], "right"),
    (["opposite", "big"], "small"),
    (["opposite", "open"], "closed"),
    (["opposite", "fast"], "slow"),
    (["opposite", "wet"], "dry"),
    (["opposite", "full"], "empty"),
    (["capital", "france"], "paris"),
    (["capital", "england"], "london"),
    (["capital", "spain"], "madrid"),
    (["capital", "italy"], "rome"),
    (["capital", "japan"], "tokyo"),
    (["capital", "germany"], "berlin"),
    (["capital", "egypt"], "cairo"),
    (["room", "sink", "dishes"], "kitchen"),
    (["room", "cook"], "kitchen"),
    (["room", "bed"], "bedroom"),
    (["room", "shower"], "bathroom"),
    (["room", "bathtub"], "bathroom"),
    (["room", "sofa"], "living room"),
    (["room", "tv"], "living room"),
    (["room", "dining"], "dining room"),
    (["color", "sky"], "blue"),
    (["color", "grass"], "green"),
    (["color", "banana"], "yellow"),
    (["color", "snow"], "white"),
    (["color", "blood"], "red"),
    (["color", "stop sign"], "red"),
    (["color", "pumpkin"], "orange"),
    (["color", "chocolate"], "brown"),
    (["color", "coal"], "black"),
    (["color", "sun"], "yellow"),
    (["use", "eat soup"], "spoon"),
    (["use", "cut paper"], "scissors"),
    (["use", "write"], "pen"),
    (["use", "tell time"], "clock"),
    (["use", "read"], "book"),
    (["use", "take pictures"], "camera"),
    (["use", "call"], "phone"),
    (["use", "light", "room"], "lamp"),
    (["use", "clean", "teeth"], "toothbrush"),
    (["use", "dry", "hands"], "towel"),
    (["use", "brush", "hair"], "brush"),
    (["use", "open", "door"], "key"),
    (["use", "see", "dark"], "flashlight"),
    (["use", "keep food cold"], "refrigerator"),
    (["use", "wash clothes"], "washing machine"),
    (["wear", "feet"], "shoes"),
    (["wear", "head"], "hat"),
    (["wear", "eyes"], "sunglasses"),
    (["wear", "hands"], "gloves"),
    (["wear", "wrist"], "watch"),
    (["fly", "sky"], "plane"),
    (["ride", "school"], "bus"),
    (["drink", "soup"], "spoon"),
    (["material", "windows"], "glass"),
    (["material", "paper"], "wood"),
    (["month", "after", "june"], "july"),
    (["month", "after", "july"], "august"),
    (["month", "first", "year"], "january"),
    (["month", "last", "year"], "december"),
    (["season", "after", "winter"], "spring"),
    (["season", "after", "spring"], "summer"),
    (["season", "after", "summer"], "autumn"),
    (["day", "after", "tuesday"], "wednesday"),
    (["day", "after", "monday"], "tuesday"),
    (["day", "before", "friday"], "thursday"),
    (["first", "day", "week"], "sunday"),
    (["which", "larger", "mouse", "horse"], "horse"),
    (["which", "larger", "cat", "elephant"], "elephant"),
    (["which", "faster", "cheetah"], "cheetah"),
    (["which", "faster", "plane", "car"], "plane"),
    (["which", "colder", "ice", "fire"], "ice"),
    (["which", "heavier", "elephant", "mouse"], "elephant"),
    (["legs", "spider"], "8"),
    (["legs", "dog"], "4"),
    (["legs", "cat"], "4"),
    (["legs", "horse"], "4"),
    (["legs", "insect"], "6"),
    (["legs", "ant"], "6"),
    (["legs", "bird"], "2"),
    (["legs", "person"], "2"),
    (["wheels", "car"], "4"),
    (["wheels", "bicycle"], "2"),
    (["eyes", "human"], "2"),
    (["fingers", "hand"], "5"),
    (["toes", "foot"], "5"),
]


def _solve_semantic(text: str) -> Optional[str]:
    """Answer via topic-keyword table — matches ANY phrasing containing the topics."""
    if not text:
        return None
    t = text.lower()
    for keywords, answer in SEMANTIC_ANSWERS:
        if all(kw in t for kw in keywords):
            return answer
    return None


def _solve_knowledge_question(text: str) -> Optional[str]:
    """Answer natural-language knowledge questions locally (no API).
    Returns the answer string or None."""
    if not text:
        return None
    t = text.lower()

    # ── Category pickers: "Which of these is a/an X?" / "...is not a X?" ──
    # Robust against "is a fruit: apple, car, tree" and "comes after" phrasing.
    for _cat_re in (
        r"which (?:one )?of (?:these|the following|the) "
        r"(?:words )?(?:is not|are not|is|are)? ?(?:an? |the )?([a-z][a-z ]{2,20}?)\s*(?::|\?|\.|$)",
        r"(?:pick the one (?:that|which)? ?(?:represents|is)|represents|is) (?:an? |the )?([a-z][a-z ]{2,20}?)\s*(?::|\?|\.|,|$)",
    ):
        cat_match = re.search(_cat_re, t)
        if not cat_match:
            continue
        cat = cat_match.group(1).strip()
        cat_key = None
        for key in CATEGORY_WORDS:
            if cat == key or cat.startswith(key) or key.startswith(cat) or cat in key:
                cat_key = key
                break
        if cat_key:
            words = re.findall(r"[a-z]+", t)
            candidates = [w for w in words if w in CATEGORY_WORDS[cat_key]]
            negated = bool(re.search(r"\bnot\b", t))
            if negated:
                # "which is not a X" → pick the word NOT in category
                stop = ("which", "these", "following", "words", "one", "that",
                        "with", "from", "the", "and", "this", "them", "their",
                        "they", "are", "not", "animal", "fruit", "vegetable",
                        "color", "colour", "room", "is", "a", "an", "of")
                all_choice = [w for w in words if len(w) >= 3 and w not in stop]
                for w in all_choice:
                    if w not in CATEGORY_WORDS[cat_key]:
                        return w
            elif candidates:
                return candidates[0]

    # ── Direct pattern match ──
    for pattern, answer in KNOWLEDGE_QUESTIONS:
        if re.search(pattern, t, re.IGNORECASE):
            return answer

    return None


def _eval_arithmetic_chain(expr: str) -> Optional[str]:
    """Evaluate a pure integer arithmetic chain ('5 + 8 + 7', '12 × 3 ÷ 2',
    '10 - 2 - 3') with standard operator precedence. Accepts ASCII and unicode
    math symbols (× ÷ − x X). Returns the answer as a string, or None when the
    expression is not a safe integer expression."""
    s = expr.replace(" ", "")
    s = s.replace("×", "*").replace("÷", "/").replace("−", "-")
    s = s.replace("x", "*").replace("X", "*")
    if not re.fullmatch(r"[0-9+\-*/]+", s):
        return None
    tokens = re.findall(r"\d+|[+\-*/]", s)
    if len(tokens) < 3 or len(tokens) % 2 == 0:
        return None
    nums: List[int] = []
    ops: List[str] = []
    for tok in tokens:
        if tok.isdigit():
            nums.append(int(tok))
        else:
            ops.append(tok)
    if len(nums) != len(ops) + 1:
        return None
    # pass 1: * and / (left to right)
    i = 0
    while i < len(ops):
        if ops[i] in ("*", "/"):
            a, b = nums[i], nums[i + 1]
            if ops[i] == "*":
                nums[i] = a * b
            else:
                if b == 0:
                    return None
                nums[i] = a // b if a % b == 0 else a / b
            del nums[i + 1]
            del ops[i]
        else:
            i += 1
    # pass 2: + and -
    val = nums[0]
    for op, b in zip(ops, nums[1:]):
        val = val + b if op == "+" else val - b
    if isinstance(val, float) and val.is_integer():
        val = int(val)
    return str(val)


# ═══════════════════════════════════════════════════════════════
# LLM few-shot prompt (module-level so the test harness exercises the
# EXACT same production prompt path: pool selection + think:false call +
# answer cleaning).
# ═══════════════════════════════════════════════════════════════

# Few-shot examples injected into the LLM prompt. Small local models
# (llama3.2:1b, qwen3:1.7b) only answer CAPTCHA trivia correctly when they
# are shown the same question TYPE first, so pick the most relevant examples
# for each question before asking.
FEWSHOT_POOL = [
    ("You start with 6 coins in a jar. On Wednesday, you put 9 coins into the jar. How many coins are in your jar now?", "15"),
    ("Your coin jar has 8 coins. On Monday, you add 9 coins. How many coins are in the jar?", "17"),
    ("What vegetable is white inside and brown outside?", "potato"),
    ("What direction does the sun set in?", "west"),
    ("What liquid do you use to wash your body?", "soap"),
    ("What do we call a container that holds coins?", "jar"),
    ("What is the capital of France?", "paris"),
    ("How many days are in a week?", "7"),
    ("What color is the sky?", "blue"),
    ("What do bees make?", "honey"),
    ("What is the largest planet in our solar system?", "jupiter"),
    ("How many legs does a spider have?", "8"),
    ("What do we call a baby dog?", "puppy"),
    ("What is the opposite of hot?", "cold"),
    ("What gas do plants absorb?", "carbon dioxide"),
    ("What instrument has 88 keys?", "piano"),
    ("What do you use to cut paper?", "scissors"),
    ("How many minutes are in an hour?", "60"),
    ("What animal says moo?", "cow"),
    ("What is the tallest land animal?", "giraffe"),
    ("What do you call the person who flies a plane?", "pilot"),
    ("What color do you get when you mix red and white?", "pink"),
    ("How many wheels does a car have?", "4"),
    ("What is the first month of the year?", "january"),
    ("What do you use to write on a blackboard?", "chalk"),
    ("What is the freezing point of water in celsius?", "0"),
    ("Can bread be stored frozen?", "yes"),
    ("Do cats meow?", "yes"),
    # ── hCaptcha trivia question TYPES (content differs from the AI
    # sweep questions so the test measures real knowledge, not leakage) ──
    ("Which country has the capital city Beijing?", "china"),
    ("What is the capital of Kenya?", "nairobi"),
    ("What is the capital of Egypt?", "cairo"),
    ("What is the national sport of England?", "cricket"),
    ("What is the smallest mammal in the world?", "bumblebee bat"),
    ("What is the largest reptile in the world?", "saltwater crocodile"),
    ("What is the tallest mountain in the world?", "everest"),
    ("What is the largest land animal?", "elephant"),
    ("What is the largest desert in the world?", "sahara"),
    ("What is the largest ocean in the world?", "pacific"),
    ("How many letters are in the word butterfly?", "9"),
    # -- User-requested extras: same question TYPES for the 3 extra
    # test questions (qwen3:1.7b refuses with /no_answer/ unless primed
    # with the exact question TYPE first) --
    ("What is the slowest animal?", "sloth"),
    ("Who is the richest man in the world?", "elon musk"),
    ("How many seconds are in an hour?", "3600"),
    ("Who is the richest woman in the world?", "francoise bettencourt meyers"),
    ("Which person has the most money on earth?", "elon musk"),
]



def _normalize_llm_question(q: str) -> str:
    """Turn terse/contracted hCaptcha fragments into full questions so small
    models (qwen3:1.7b) answer instead of emitting /NoAnswer/ refusals.
    Examples:
      'Slowest animal'                  -> 'What is the slowest animal?'
      "Who's the richest person on earth" -> 'Who is the richest person on earth?'
    """
    s = (q or "").strip()
    if not s:
        return q or ""
    # Expand common contractions (small models misread "who's" as a name/refusal).
    for a, b in (("who's", "who is"), ("what's", "what is"), ("how's", "how is"),
                 ("where's", "where is"), ("when's", "when is"), ("why's", "why is"),
                 ("it's", "it is"), ("there's", "there is"), ("that's", "that is"),
                 ("don't", "do not"), ("doesn't", "does not"), ("can't", "cannot")):
        s = re.sub(r"\b" + re.escape(a) + r"\b", b, s, flags=re.IGNORECASE)
    # Bare fragment (no question starter) -> "What is the ..."
    if not re.match(
        r"^(?:what|which|who|whom|whose|how|when|where|why|is|are|was|were|"
        r"do|does|did|can|could|would|should|may|might|has|have|had|shall|will)\b",
        s, re.IGNORECASE):
        s = "What is the " + s.lower()
    # Ensure it reads as a question (trailing '?').
    s = re.sub(r"[?.!]+$", "", s).strip()
    return s + "?"


def build_llm_prompt(question: str) -> str:
    """Pick the 4 most relevant few-shot examples and build the prompt."""
    question = _normalize_llm_question(question)
    stop = {"what", "which", "how", "many", "much", "does", "do", "is", "are",
            "the", "and", "with", "your", "you", "that", "this", "from", "into",
            "there", "have", "has", "can", "would", "about", "when", "where",
            "its", "answer", "question", "following", "single", "word", "number",
            "phrase", "please", "put", "add", "call", "calls", "one", "using"}
    qw = {w for w in re.findall(r"[a-z]{3,}", question.lower()) if w not in stop}
    # Prefer examples of the same question TYPE: same opener word
    # (what/who/how/which), same superlative structure, and NEVER
    # arithmetic (coin) examples unless the question itself is about coins
    # — a 1.7B model refuses (/NoAnswer/) when the few-shot mix spans
    # unrelated domains (verified live).
    scored = []
    _q_first = (question.split() or [""])[0].lower()
    _q_superl = re.search(
        r"(largest|biggest|smallest|tallest|highest|longest|fastest|slowest|"
        r"richest|oldest|youngest|deepest|hottest|coldest|most|least)",
        question.lower())
    for eq, ea in FEWSHOT_POOL:
        ew = {w for w in re.findall(r"[a-z]{3,}", eq.lower())}
        score = len(qw & ew)
        if (eq.split() or [""])[0].lower() == _q_first:
            score += 3
        if _q_superl and re.search(
                r"(largest|biggest|smallest|tallest|highest|longest|fastest|"
                r"slowest|richest|oldest|youngest|deepest|hottest|coldest|"
                r"most|least)", eq.lower()):
            score += 2
        if "coin" in eq and "coin" not in question.lower():
            score -= 4
        scored.append((score, eq, ea))
    scored.sort(key=lambda x: -x[0])
    lines = ["Answer each question with exactly ONE word or number.",
             "No punctuation, no explanation, no quotes."]
    for _score, eq, ea in scored[:4]:
        lines.append("Question: " + eq)
        lines.append("Answer: " + ea)
    lines.append("Question: " + question)
    lines.append("Answer:")
    return "\n".join(lines)


def clean_llm_answer(raw: str) -> str:
    """Normalize an LLM answer: lowercase, strip punctuation and
    rambling preambles, keep up to 3 words (captcha answers can be
    phrases like 'dog food' or 'living room'). Returns '' if empty."""
    if not raw:
        return ""
    # Lowercase, drop quotes/brackets/periods but keep word separators
    s = re.sub(r"[\"'`\[\](){}<>]", "", raw)
    s = s.replace(".", " ").replace(",", " ").replace(";", " ").replace(":", " ")
    s = s.replace("\n", " ").replace("\t", " ").replace("-", " ")
    s = s.lower()
    # Strip rambling preambles repeatedly so the answer word survives:
    # "i think the answer is X", "it is X", "probably X", "my answer is X"
    _preamble = re.compile(
        r'^(?:(?:i\s+(?:think|believe|guess|would\s+say|am\s+pretty\s+sure))'
        r'|(?:the\s+answer\s+(?:is|would\s+be))'
        r'|(?:the\s+(?:correct|right)\s+answer\s+(?:is|would\s+be))'
        r'|(?:my\s+answer\s+is)'
        r'|(?:that\s+would\s+be)'
        r'|(?:it\s+is)'
        r'|(?:it\'?s)'
        r'|(?:the\s+word\s+is)'
        r'|(?:this\s+is)'
        r'|(?:probably|maybe|likely|definitely|obviously))'
        r'\b[\s,:;-]*')
    for _ in range(3):
        if not s:
            break
        s2 = _preamble.sub('', s)
        if s2 == s:
            break
        s = s2
    words = [w for w in s.split() if re.search(r"[a-z0-9]", w)]
    if not words:
        return ""
    # Drop filler words that sometimes leak out
    stop = {"the", "a", "an", "is", "are", "it", "of", "to", "in", "for",
            "answer", "with", "and", "or", "be", "please",
            "i", "think", "believe", "guess", "probably", "maybe", "likely",
            "would", "should", "could", "that", "this", "its", "correct", "right",
            "my", "so", "just", "really", "very", "most"}
    cleaned = [w for w in words if w not in stop]
    if not cleaned:
        return ""
    return " ".join(cleaned[:3])


async def _dump_clickables(page, frame, iframe_box, log):
    """Log EVERY clickable element found (iframe + page) with coordinates.
    Used for debugging — shows exactly what the bot sees and where."""
    try:
        page_scroll = await page.evaluate("() => ({x: window.scrollX || 0, y: window.scrollY || 0})")
        log(f"[Accessibility] Page scroll: ({page_scroll['x']}, {page_scroll['y']})")
    except Exception:
        page_scroll = {"x": 0, "y": 0}

    # Dump iframe clickables
    try:
        items = await frame.evaluate("""() => {
            const out = [];
            const els = document.querySelectorAll('button, [role="button"], a, [aria-label], [title], [class*="dot"], [class*="menu"]');
            for (const el of els) {
                if (el.offsetParent === null) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 5 || r.height < 5) continue;
                const t = (el.textContent || '').trim().slice(0, 25);
                const label = el.getAttribute('aria-label') || el.getAttribute('title') || '';
                const cls = (el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className) || '';
                out.push({
                    x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2),
                    w: Math.round(r.width), h: Math.round(r.height),
                    tag: el.tagName, text: t.slice(0, 20), label: label.slice(0, 30),
                    cls: String(cls).slice(0, 40),
                });
            }
            return out;
        }""")
        log(f"[Accessibility] IFRAME clickables ({len(items or [])}):")
        for it in (items or [])[:25]:
            log(f"[Accessibility]   iframe ({it['x']},{it['y']}) {it['w']}x{it['h']} "
                f"<{it['tag']}> label='{it['label']}' text='{it['text']}' cls='{it['cls']}'")
    except Exception as e:
        log(f"[Accessibility] iframe dump error: {e}", level="warn")

    # Dump page clickables (inside the hcaptcha widget container)
    try:
        items = await page.evaluate("""() => {
            const out = [];
            const scope = document.querySelector('[class*="hcaptcha"], [id*="hcaptcha"], iframe[src*="hcaptcha"]')
                          ? document : document.body;
            const els = scope.querySelectorAll('button, [role="button"], a, [aria-label], [class*="dot"], [class*="menu"]');
            for (const el of els) {
                if (el.offsetParent === null) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 5 || r.height < 5) continue;
                const t = (el.textContent || '').trim().slice(0, 25);
                const label = el.getAttribute('aria-label') || el.getAttribute('title') || '';
                const cls = (el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className) || '';
                out.push({
                    x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2),
                    w: Math.round(r.width), h: Math.round(r.height),
                    tag: el.tagName, text: t.slice(0, 20), label: label.slice(0, 30),
                    cls: String(cls).slice(0, 40),
                });
            }
            return out;
        }""")
        log(f"[Accessibility] PAGE clickables ({len(items or [])}):")
        for it in (items or [])[:25]:
            log(f"[Accessibility]   page ({it['x']},{it['y']}) {it['w']}x{it['h']} "
                f"<{it['tag']}> label='{it['label']}' text='{it['text']}' cls='{it['cls']}'")
    except Exception as e:
        log(f"[Accessibility] page dump error: {e}", level="warn")


async def _click_at(page, x, y, log, desc=""):
    """Real mouse click at scroll-aware page coordinates."""
    try:
        scroll = await page.evaluate("() => window.scrollY || 0")
    except Exception:
        scroll = 0
    log(f"[Accessibility] Clicking {desc} at ({x:.0f},{y:.0f}) (scrollY={scroll:.0f})")
    await page.mouse.click(x, y)


async def solve_hcaptcha_accessibility(page, iframe, 
                                        ollama_model: str = "",
                                        ollama_url: str = "",
                                        log: Optional[Callable] = None,
                                        max_attempts: int = 3,
                                        max_questions: int = 6) -> bool:
    """Solve hCaptcha via the Accessibility Challenge using Playwright's
    frame_locator for reliable cross-origin iframe interaction.

    Flow:
      1. Use frame_locator('iframe[title="hCaptcha challenge"]') for iframe.
      2. Click #menu-info (the 3-dots) inside the hCaptcha iframe.
      3. Select "Accessibility Challenge".
      4. Read the question TEXT from the page; solve with local
         solvers first, then Ollama text LLM (no screenshots).
      5. Type answer and submit.

    Requirements:
      - Ollama running with a text model (e.g. `ollama pull llama3.2`)
      - Set OLLAMA_URL when Ollama is not on the same machine
    """
    log = log or (lambda msg, level="info": None)

    # ── Humanization knobs ─────────────────────────────────────────────
    # Machine-perfect, instant answers are what hCaptcha uses to grade the
    # session as a bot — which is what then pushes Discord into the phone
    # verification lock. These add human cadence and imperfection.
    HUMAN_THINK_MIN = float((os.environ.get("HUMAN_THINK_MIN") or "0.8") or "0.8")
    HUMAN_THINK_MAX = float((os.environ.get("HUMAN_THINK_MAX") or "2.4") or "2.4")
    HUMAN_MISTAKE_RATE = float((os.environ.get("HUMAN_MISTAKE_RATE") or "0.08") or "0.08")
    HUMAN_MISTAKE_RATE = max(0.0, min(1.0, HUMAN_MISTAKE_RATE))

    # Ollama endpoint/model come from env vars so the bot can reach
    # a server that actually hosts a vision model (localhost:11434
    # only works when Ollama runs on the same machine as the bot).
    if not ollama_url:
        ollama_url = os.environ.get("OLLAMA_URL") or os.environ.get("OLLAMA_BASE") or "http://localhost:11434"
    if not ollama_model:
        ollama_model = os.environ.get("OLLAMA_MODEL") or os.environ.get("OLLAMA_VISION_MODEL") or "qwen3:1.7b"
    ollama_url = ollama_url.rstrip("/")
    log(f"[Accessibility] Ollama endpoint: {ollama_url}  model: {ollama_model}")
    import asyncio
    import base64

    async def _discover_text_model() -> str:
        """Pick a TEXT-capable model for answering questions as text.
        Vision-only models (moondream, llava, minicpm-v) answer text prompts
        poorly, so prefer real chat models if the server has any."""
        try:
            import aiohttp
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=3)
            ) as session:
                async with session.get(f"{ollama_url}/api/tags") as resp:
                    if resp.status != 200:
                        return ollama_model
                    data = await resp.json()
            names = [m.get("name", "") for m in data.get("models", [])]
            if not names:
                return ollama_model
            vision_only = {"moondream", "llava", "llava-llama3", "bakllava",
                           "minicpm-v", "minicpm-v:8b"}
            text_names = [n for n in names
                          if n.split(":")[0] not in
                          {v.split(":")[0] for v in vision_only}]
            candidates = text_names or names
            # User runs qwen3:1.7b — land on any qwen3 1.x before the
            # generic prefix list so discovery picks it every time.
            for n in sorted(candidates, key=lambda x: -len(x)):
                base, _, tag = n.partition(":")
                if base == "qwen3" and (tag.startswith("1") or tag.startswith("0.")):
                    return n
            # qwen3:1.7b is the ONLY text model — all other fallback
            # models (qwen2.x, gemma, llama, mistral, phi, deepseek,
            # tinyllama, dolphin, orca-mini) were removed by request.
            if ollama_model:
                return ollama_model
            return "qwen3:1.7b"
        except Exception:
            return ollama_model

    # Text model for answering questions as text (separate from the vision
    # model used only when NO text is extractable from the page).
    ollama_text_model = (os.environ.get("OLLAMA_TEXT_MODEL") or "").strip()
    if not ollama_text_model:
        ollama_text_model = await _discover_text_model()
    if not ollama_text_model:
        ollama_text_model = ollama_model or 'qwen3:1.7b' 
    log(f"[Accessibility] Text model in use: {ollama_text_model}")
    if ollama_text_model.split(":")[0].lower() in {"moondream", "llava", "minicpm-v", "bakllava"}:
        log("[Accessibility] [WARN] Only a VISION model is available — text questions may get "
            "empty answers. Pull a text model (e.g. `ollama pull llama3.2`) on the Ollama server "
            "or set OLLAMA_TEXT_MODEL.", level="warn")

    # Pre-warm the text model so the first unknown question doesn't pay a
    # cold-start timeout (a 1.3GB model takes seconds to load on first call).
    try:
        import aiohttp
        warm_payload = {
            "model": ollama_text_model or ollama_model,
            "stream": False,
            "keep_alive": "30m",
            "think": False,
            "options": {"num_predict": 1},
            "messages": [{"role": "user", "content": "hi"}],
        }

        async def _warm_model() -> int:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8)
            ) as s:
                async with s.post(f"{ollama_url}/api/chat", json=warm_payload) as r:
                    return r.status

        log(f"[Accessibility] Warming text model {ollama_text_model or ollama_model}...")
        _warm_status = await _warm_model()
        log(f"[Accessibility] Text model warm (status {_warm_status})")
    except Exception as _warm_err:
        log(f"[Accessibility] Warm-up skipped: {_warm_err}", level="warn")


    # ── VISION solver (Qwen3-VL family via Ollama) ─────────────────────
    # Used for IMAGE challenges (drag / silhouette / image-pick) with EXACT
    # pixel coordinates, and as a final accuracy layer for ambiguous text
    # questions. Set OLLAMA_VISION_MODEL on the Ollama server, e.g.
    #   ollama pull qwen2.5vl:7b   (or qwen3-vl:8b when available)
    #   OLLAMA_VISION_MODEL=qwen2.5vl:7b
    ollama_vision_model = __import__("os").environ.get("OLLAMA_VISION_MODEL") or ""
    ollama_vision_model = ollama_vision_model.strip()
    if ollama_vision_model:
        log("[Vision] Image solver armed: " + ollama_vision_model + " (exact-coordinate drag/image solving)")
    else:
        log("[Vision] No OLLAMA_VISION_MODEL set - image/drag challenges fall back to Skip")

    # Every vision solve is appended to data/vision_train.jsonl so the
    # Qwen3-VL LoRA trainer can be retrained on every real challenge.
    _VISION_TRAIN_FILE = "data/vision_train.jsonl"

    def _save_vision_sample(image_b64, question, model_reply, plan):
        try:
            __import__("os").makedirs("data", exist_ok=True)
            with open(_VISION_TRAIN_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "question": (question or "")[:300],
                    "screenshot_b64": image_b64,
                    "model_reply": (model_reply or "")[:600],
                    "plan": plan,
                }) + "\n")
        except Exception:
            pass

    async def _llm_answer_vision(image_b64, prompt, timeout=25.0):
        """Ask the Ollama VISION model about a screenshot. Returns raw text."""
        if not ollama_vision_model:
            return ""
        try:
            import aiohttp
            payload = {
                "model": ollama_vision_model,
                "stream": False,
                "keep_alive": "30m",
                "think": False,
                "options": {"temperature": 0.1, "num_predict": 320},
                "messages": [{"role": "user", "content": prompt,
                              "images": [image_b64]}],
            }
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as session:
                async with session.post(ollama_url + "/api/chat",
                                        json=payload) as resp:
                    if resp.status != 200:
                        log("[Vision] Ollama vision HTTP " + str(resp.status), level="warn")
                        return ""
                    data = await resp.json()
                    return str(data.get("message", {}).get("content", "")).strip()
        except asyncio.TimeoutError:
            return ""
        except Exception as e:
            log("[Vision] Ollama vision error: " + str(e), level="warn")
            return ""

    _VISION_PROMPT = (
        "You are an expert hCaptcha solver. You are shown a screenshot of an "
        "hCaptcha challenge. Pixel coordinates are relative to the image "
        "(0,0 = top-left). Determine the challenge type and reply with STRICT "
        "JSON only - no markdown, no extra text, no code fences:\n"
        '{"type": "drag", "from": [x,y], "to": [x,y]}\n'
        '{"type": "click", "points": [[x,y], ...]}\n'
        '{"type": "text", "answer": "single word"}\n'
        "Rules:\n"
        "- drag: the puzzle piece you must grab is centered at [from]; the "
        "target drop zone (the outlined silhouette/slot it must land in) is "
        "centered at [to]. Give CENTER pixels of each, exact integers.\n"
        "- click: give the center pixel of EVERY image/tile you must click "
        "(the ones matching the prompt), exact integers.\n"
        "- text: the single word/number/phrase that answers the question "
        "shown (the challenge is displayed as an image).\n"
        "Be exact - wrong coordinates fail the challenge."
    )

    def _parse_vision_json(raw):
        if not raw:
            return None
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    async def _execute_drag(fx, fy, tx, ty):
        """Trusted CDP mouse drag with a slight arc (piece -> target)."""
        try:
            await page.mouse.move(fx, fy, steps=14)
            await asyncio.sleep(0.12)
            await page.mouse.down()
            steps = 20
            for i in range(1, steps + 1):
                t = i / steps
                # gentle vertical arc so the path isn't a dead-straight line
                arc = math.sin(t * math.pi) * 6
                await page.mouse.move(fx + (tx - fx) * t,
                                      fy + (ty - fy) * t - arc, steps=1)
                await asyncio.sleep(0.007)
            await asyncio.sleep(0.1)
            await page.mouse.up()
        except Exception as e:
            log("[Vision] drag execution error: " + str(e), level="warn")

    async def _solve_gesture_challenge(question_text):
        """Screenshot the viewport, ask the vision model for exact
        coordinates, execute the gesture. Returns True when performed."""
        if not ollama_vision_model:
            return False
        try:
            # Viewport capture (not full-page) so model pixel coordinates
            # map 1:1 to viewport mouse coordinates for the drag/click.
            try:
                _png = await page.screenshot(full_page=False, type="png")
                shot = base64.b64encode(_png).decode() if _png else ""
            except Exception:
                shot = await _screenshot_b64(page)
            if not shot:
                return False
            prompt = _VISION_PROMPT + (
                "\n\nChallenge instruction shown to the user: " +
                (question_text or "")[:220])
            raw = await _llm_answer_vision(shot, prompt, timeout=25.0)
            plan = _parse_vision_json(raw)
            if not plan:
                log("[Vision] No parseable plan: " + str(raw)[:120], level="warn")
                return False
            ctype = str(plan.get("type", "")).lower()
            _save_vision_sample(shot, question_text, raw, plan)
            if ctype == "drag":
                frm = plan.get("from")
                to = plan.get("to")
                if (isinstance(frm, (list, tuple)) and len(frm) >= 2
                        and isinstance(to, (list, tuple)) and len(to) >= 2):
                    log("[Vision] Drag %d,%d -> %d,%d" % (int(frm[0]), int(frm[1]), int(to[0]), int(to[1])))
                    await _execute_drag(float(frm[0]), float(frm[1]),
                                        float(to[0]), float(to[1]))
                    return True
            elif ctype == "click":
                pts = plan.get("points") or []
                if isinstance(pts, list) and pts:
                    for p in pts[:8]:
                        if isinstance(p, (list, tuple)) and len(p) >= 2:
                            log("[Vision] Click %d,%d" % (int(p[0]), int(p[1])))
                            await page.mouse.click(float(p[0]), float(p[1]))
                            await asyncio.sleep(0.35)
                    return True
            return False
        except Exception as e:
            log("[Vision] gesture solve error: " + str(e), level="warn")
            return False

    # ── Helpers ────────────────────────────────────────────

    async def _ollama_chat(image_b64: str, prompt: str, timeout: float = 20.0) -> str:
        """Send image + prompt to Ollama /api/chat (vision models)."""
        try:
            import aiohttp
            payload = {
                "model": ollama_model,
                "stream": False,
                "messages": [{
                    "role": "user",
                    "content": prompt,
                    "images": [image_b64],
                }],
            }
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as session:
                async with session.post(
                    f"{ollama_url}/api/chat", json=payload
                ) as resp:
                    if resp.status != 200:
                        return ""
                    data = await resp.json()
                    return data["message"]["content"].strip()
        except Exception as e:
            log(f"[Accessibility] Ollama error: {e}", level="error")
            return ""

    # Few-shot pool lives at module level (FEWSHOT_POOL) so the test
    # harness exercises the exact same production prompt path.
    _FEWSHOT_POOL = FEWSHOT_POOL

    def _build_llm_prompt(question: str) -> str:
        """Pick the 4 most relevant few-shot examples and build the prompt."""
        question = _normalize_llm_question(question)
        stop = {"what", "which", "how", "many", "much", "does", "do", "is", "are",
                "the", "and", "with", "your", "you", "that", "this", "from", "into",
                "there", "have", "has", "can", "would", "about", "when", "where",
                "its", "answer", "question", "following", "single", "word", "number",
                "phrase", "please", "put", "add", "call", "calls", "one", "using"}
        qw = {w for w in re.findall(r"[a-z]{3,}", question.lower()) if w not in stop}
        # Same-type preference (see module-level build_llm_prompt): opener
        # word + superlative bonus, arithmetic/coin penalty.
        scored = []
        _q_first = (question.split() or [""])[0].lower()
        _q_superl = re.search(
            r"(largest|biggest|smallest|tallest|highest|longest|fastest|"
            r"slowest|richest|oldest|youngest|deepest|hottest|coldest|most|"
            r"least)", question.lower())
        for eq, ea in _FEWSHOT_POOL:
            ew = {w for w in re.findall(r"[a-z]{3,}", eq.lower())}
            score = len(qw & ew)
            if (eq.split() or [""])[0].lower() == _q_first:
                score += 3
            if _q_superl and re.search(
                    r"(largest|biggest|smallest|tallest|highest|longest|"
                    r"fastest|slowest|richest|oldest|youngest|deepest|"
                    r"hottest|coldest|most|least)", eq.lower()):
                score += 2
            if "coin" in eq and "coin" not in question.lower():
                score -= 4
            scored.append((score, eq, ea))
        scored.sort(key=lambda x: -x[0])
        lines = ["Answer each question with exactly ONE word or number.",
                 "No punctuation, no explanation, no quotes."]
        for _score, eq, ea in scored[:4]:
            lines.append("Question: " + eq)
            lines.append("Answer: " + ea)
        lines.append("Question: " + question)
        lines.append("Answer:")
        return "\n".join(lines)

    async def _ollama_answer_text(question: str, timeout: float = 12.0) -> str:
        """Ask Ollama (text-only chat) for a single-word answer to a question.
        Uses few-shot examples + a majority vote (2 samples, tiebreak with a
        3rd) so small local models answer correctly instead of guessing.
        Default is 1 vote (fastest). Set OLLAMA_VOTES=2+ for majority
        voting - only useful on a fast server."""
        try:
            votes = max(1, int(os.environ.get("OLLAMA_VOTES", "1") or "1"))
        except Exception:
            votes = 1
        # Each vote gets the FULL timeout budget. A single-GPU Ollama
        # serializes parallel requests, so the old timeout/votes budget made
        # EVERY vote time out on slow instances - the logs' "2 x 22s".
        per_sample = max(8.0, timeout + 3.0)

        async def _post() -> str:
            import aiohttp
            payload = {
                "model": ollama_text_model or ollama_model,
                "stream": False,
                "keep_alive": "30m",
                # Qwen3 has thinking mode ON by default: with stop=["\n", "."]
                # the model is cut off mid-thought and returns EMPTY content.
                # think must be a TOP-LEVEL request field (verified live).
                "think": False,
                "options": {"temperature": 0.2, "num_predict": 16,
                            "stop": ["\n", "."]},
                "messages": [{"role": "user", "content": _build_llm_prompt(question)}],
            }
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=per_sample)
                ) as session:
                    async with session.post(
                        f"{ollama_url}/api/chat", json=payload
                    ) as resp:
                        if resp.status != 200:
                            log(f"[Accessibility] Ollama text HTTP {resp.status}", level="warn")
                            return ""
                        data = await resp.json()
                        return _clean_llm_answer(data["message"]["content"])
            except asyncio.TimeoutError:
                return ""  # too slow - reported by the caller below
            except Exception as e:
                log(f"[Accessibility] Ollama text request error: {e}", level="warn")
                return ""

        # Fire all votes concurrently; return the FIRST non-empty answer and
        # cancel the stragglers. Waiting for every vote to finish (majority
        # voting) is exactly what produced the repeated "2 x 22s" timeouts on
        # a slow single-GPU server.
        pending = [asyncio.ensure_future(_post()) for _ in range(votes)]
        answers = []
        try:
            while pending and not answers:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    try:
                        v = t.result()
                    except Exception:
                        continue
                    if isinstance(v, str) and v:
                        answers.append(v)
            if answers:
                return answers[0]
            log("[Accessibility] Ollama text: all votes empty/timed out "
                f"({votes} x {per_sample:.0f}s) - model too slow or unreachable",
                level="warn")
            return ""
        except Exception as e:
            log(f"[Accessibility] Ollama text error: {e}", level="warn")
            return ""
        finally:
            for t in pending:
                t.cancel()


    def _clean_llm_answer(raw: str) -> str:
        """Normalize an LLM answer: lowercase, strip punctuation and
        rambling preambles, keep up to 3 words (captcha answers can be
        phrases like 'dog food' or 'living room'). Returns '' if empty."""
        if not raw:
            return ""
        # Lowercase, drop quotes/brackets/periods but keep word separators
        s = re.sub(r"[\"'`\[\](){}<>]", "", raw)
        s = s.replace(".", " ").replace(",", " ").replace(";", " ").replace(":", " ")
        s = s.replace("\n", " ").replace("\t", " ").replace("-", " ")
        s = s.lower()
        # Strip rambling preambles repeatedly so the answer word survives:
        # "i think the answer is X", "it is X", "probably X", "my answer is X"
        _preamble = re.compile(
            r'^(?:(?:i\s+(?:think|believe|guess|would\s+say|am\s+pretty\s+sure))'
            r'|(?:the\s+answer\s+(?:is|would\s+be))'
            r'|(?:the\s+(?:correct|right)\s+answer\s+(?:is|would\s+be))'
            r'|(?:my\s+answer\s+is)'
            r'|(?:that\s+would\s+be)'
            r'|(?:it\s+is)'
            r'|(?:it\'?s)'
            r'|(?:the\s+word\s+is)'
            r'|(?:this\s+is)'
            r'|(?:probably|maybe|likely|definitely|obviously))'
            r'\b[\s,:;-]*')
        for _ in range(3):
            if not s:
                break
            s2 = _preamble.sub('', s)
            if s2 == s:
                break
            s = s2
        words = [w for w in s.split() if re.search(r"[a-z0-9]", w)]
        if not words:
            return ""
        # Drop filler words that sometimes leak out
        stop = {"the", "a", "an", "is", "are", "it", "of", "to", "in", "for",
                "answer", "with", "and", "or", "be", "please",
                "i", "think", "believe", "guess", "probably", "maybe", "likely",
                "would", "should", "could", "that", "this", "its", "correct", "right",
                "my", "so", "just", "really", "very", "most"}
        cleaned = [w for w in words if w not in stop]
        if not cleaned:
            return ""
        return " ".join(cleaned[:3])

    async def _llm_answer_question(question: str, timeout: float = 12.0) -> str:
        """Layer 3: ask ANY LLM for the answer to an unknown question.
        Tries in order (each with up to 2 retries):
        1. Ollama (OLLAMA_URL env)
        2. OpenAI-compatible endpoint (LLM_API_URL + LLM_API_KEY + LLM_MODEL env)
        Returns the cleaned answer or empty string."""
        import asyncio

        question = (question or "").strip()[:500]
        if not question:
            return ""

        api_url = os.environ.get("LLM_API_URL") or os.environ.get("OPENAI_BASE_URL") or ""
        api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        model = os.environ.get("LLM_MODEL") or "gpt-4o-mini"

        # Log once if NOTHING is configured — user needs to know Layer 3 is inert
        if not ollama_url and not api_url:
            log("[Accessibility] [WARN] Unknown question and NO LLM configured — "
                "set OLLAMA_URL or LLM_API_URL/LLM_API_KEY/LLM_MODEL to solve any question",
                level="warn")
            return ""

        # SINGLE attempt: on a slow Ollama a retry just doubles the timeout
        # burn (logs showed 'LLM returned nothing - retrying once...' costing
        # 2x44s on the same question). The Skip button handles failures.
        for attempt in range(1, 2):  # one attempt per provider round
            # ── Option 1: Hosted OpenAI-compatible endpoint (smartest) ──
            # Tried BEFORE Ollama when configured: a hosted model (e.g.
            # gpt-4o-mini) actually knows the facts a tiny local model
            # (llama3.2:1b) can only guess at, so it should answer first.
            if api_url:
                try:
                    import aiohttp
                    endpoint = api_url.rstrip("/")
                    if not endpoint.endswith("/chat/completions"):
                        endpoint += "/chat/completions"
                    headers = {"Content-Type": "application/json"}
                    if api_key:
                        headers["Authorization"] = "Bearer " + api_key
                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": (
                                "You are solving a CAPTCHA accessibility question. "
                                "Answer with exactly ONE word, number, or short phrase. "
                                "No punctuation, no explanation, no quotes, lowercase."
                            )},
                            {"role": "user", "content": _build_llm_prompt(question)},
                        ],
                        "temperature": 0,
                        "max_tokens": 20,
                        "stop": ["\n"],
                    }
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=timeout)
                    ) as session:
                        async with session.post(endpoint, json=payload, headers=headers) as resp:
                            if resp.status != 200:
                                log(f"[Accessibility] LLM API error {resp.status}", level="warn")
                            else:
                                data = await resp.json()
                                raw = data["choices"][0]["message"]["content"]
                                cleaned = _clean_llm_answer(raw)
                                if cleaned:
                                    return cleaned
                except Exception as e:
                    log(f"[Accessibility] LLM API error: {e}", level="warn")

            # ── Option 2: Ollama (self-hosted, fallback) ──
            if ollama_url:
                ans = await _ollama_answer_text(question, timeout=timeout)
                cleaned = _clean_llm_answer(ans)
                if cleaned:
                    return cleaned

        return ""

    async def _screenshot_b64(target, selector: str | None = None) -> str:
        """Capture a screenshot as base64 PNG."""
        if selector:
            try:
                el = await target.locator(selector).first.element_handle(timeout=4000)
                if el:
                    img = await el.screenshot(type="png", timeout=4000)
                    return base64.b64encode(img).decode()
            except Exception:
                pass
        # FrameLocator doesn't have .screenshot() -- use locator("body") instead.
        # Also: locator screenshots can timeout (30s) waiting for element
        # stability inside cross-origin iframes. Use short timeout + fall back
        # to the reliable page-level screenshot on any failure.
        try:
            img = await target.screenshot(type="png", timeout=8000)
        except AttributeError:
            try:
                img = await target.locator("body").screenshot(type="png", timeout=8000)
            except Exception:
                img = await page.screenshot(type="png")
        except Exception:
            img = await page.screenshot(type="png")
        return base64.b64encode(img).decode()

    async def _screenshot_question(hcaptcha) -> str:
        """Screenshot just the question area (bigger = better OCR).
        Tries selectors inside the frame, then falls back to full frame."""
        for sel in (
            '#prompt-text', '.challenge-prompt', '[class*="prompt"]',
            '[class*="challenge"] [class*="text"]', '[class*="question"]',
            '[class*="task"]', '[class*="instruction"]', '[class*="challenge-container"]',
        ):
            try:
                el = await hcaptcha.locator(sel).first.element_handle(timeout=1500)
                if el:
                    img = await el.screenshot(type="png", timeout=4000)
                    b64 = base64.b64encode(img).decode()
                    if b64:
                        return b64
            except Exception:
                continue
        return await _screenshot_b64(hcaptcha)

    async def _token_present() -> bool:
        try:
            tok = await page.evaluate(
                """() => {
                    const ta = document.querySelector('textarea[name="h-captcha-response"]');
                    return !!(ta && ta.value && ta.value.length > 20);
                }"""
            )
            return bool(tok)
        except Exception:
            return False

    async def _challenge_js(js: str, arg=None):
        """Run JS in hCaptcha frames first, then every other frame."""
        try:
            for f in page.frames:
                try:
                    if f.url and "hcaptcha" in f.url.lower():
                        res = await f.evaluate(js, arg)
                        if res is not None and res != "":
                            return res
                except Exception:
                    continue
            for f in page.frames:
                try:
                    res = await f.evaluate(js, arg)
                    if res is not None and res != "":
                        return res
                except Exception:
                    continue
        except Exception:
            pass
        return None

    async def _accessibility_active(hcaptcha) -> bool:
        """True when the accessibility challenge UI is actually visible.
        Uses progressively broader selectors — from tight input selectors
        to container/text-based detection — to catch all hCaptcha
        accessibility variants (text input, start screen, cookie prompt).
        No page-level JS fallback (hidden hCaptcha token inputs false-match)."""
        # ── Tier 1: Direct input selectors (most specific) ──
        tier1 = [
            'input[type="text"]',
            'input[type="number"]',
            'textarea',
            '[role="textbox"]',
        ]
        for sel in tier1:
            try:
                await hcaptcha.locator(sel).first.wait_for(
                    state="visible", timeout=1000
                )
                return True
            except Exception:
                continue

        # ── Tier 2: Accessibility-specific containers & start elements ──
        tier2 = [
            '[class*="accessibility"]',
            '[class*="challenge-container"]',
            '#prompt-text',
            '[class*="prompt"]',
            'h2:has-text("Accessibility")',
            'button:has-text("Set Accessibility Cookie")',
            'button:has-text("Start")',
            '[class*="challenge-text"]',
            '[class*="task-text"]',
            '[class*="instruction"]',
            '[class*="question"]',
        ]
        for sel in tier2:
            try:
                await hcaptcha.locator(sel).first.wait_for(
                    state="visible", timeout=800
                )
                return True
            except Exception:
                continue

        # ── Tier 3: JS-based text content scan inside the hCaptcha frame ──
        # Look for any visible element containing accessibility-challenge
        # text patterns, even if the markup uses unexpected classes.
        try:
            result = await _challenge_js("""() => {
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_ELEMENT
                );
                let node;
                while ((node = walker.nextNode())) {
                    if (node.offsetParent === null) continue;
                    const t = (node.textContent || '').trim();
                    if (t.length < 3 || t.length > 200) continue;
                    if (/how many|jar|coins|add|put|total|remove|first|last|
                         letter|reverse|word|type|number|accessibility|
                         challenge|question|answer|submit/i.test(t)) {
                        return 'found';
                    }
                }
                return null;
            }""")
            if result:
                return True
        except Exception:
            pass

        return False

    async def _menu_visible(hcaptcha) -> bool:
        """True when the dropdown menu is open — has visible menu/listbox."""
        try:
            result = await _challenge_js("""() => {
                const els = document.querySelectorAll('[role="menu"], [role="listbox"], .menu, .dropdown, [class*="menu"]');
                for (const el of els) {
                    if (el.offsetParent !== null && el.children.length > 0) {
                        return 'menu_open';
                    }
                }
                return null;
            }""")
            return bool(result)
        except Exception:
            return False

    async def _click_three_dots(hcaptcha) -> bool:
        """Click the 3-dots menu button — tries 4 methods in rapid sequence:
        WAY 1: JS evaluate finds & clicks the button (most direct, bypasses intercept).
        WAY 2: Playwright aria-label / role-based click.
        WAY 3: CSS selector #menu-info + force-click.
        WAY 4: Dispatch click event as last resort.
        Only waits 1.2s between attempts; the widget has already loaded."""

        # ── WAY 1: JS evaluation (most reliable — bypasses intercept layers) ──
        try:
            js_result = await _challenge_js("""() => {
                const btn = document.querySelector('#menu-info')
                         || document.querySelector('[aria-label*="About hCaptcha"]')
                         || document.querySelector('[aria-label*="Extra menu"]')
                         || document.querySelector('.display-menu-btn');
                if (btn && btn.offsetParent !== null) {
                    btn.scrollIntoView({block: 'center'});
                    btn.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
                    btn.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
                    btn.dispatchEvent(new MouseEvent('click', {bubbles:true}));
                    return 'js_click_ok';
                }
                return null;
            }""")
            if js_result:
                log("[Accessibility] Clicked 3-dots via JS (way 1)")
                await asyncio.sleep(0.5)
                return True
        except Exception as e:
            log(f"[Accessibility] way 1 (JS) failed: {str(e)[:80]}", level="warn")

        await asyncio.sleep(0.6)

        # ── WAY 2: Playwright aria-label / role click ──
        try:
            for label in ("About hCaptcha & Accessibility Options",
                          "Extra menu", "More options", "Menu"):
                try:
                    btn = hcaptcha.get_by_role("button", name=label).first
                    await btn.wait_for(state="visible", timeout=3000)
                    await btn.click(timeout=2000)
                    log(f"[Accessibility] Clicked 3-dots via role '{label}' (way 2)")
                    await asyncio.sleep(0.5)
                    return True
                except Exception:
                    continue
            # generic label fallback
            btn = hcaptcha.get_by_label("Extra menu").first
            await btn.wait_for(state="visible", timeout=2000)
            await btn.click(timeout=2000)
            log("[Accessibility] Clicked 3-dots via aria-label (way 2)")
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            log(f"[Accessibility] way 2 (aria-label) failed: {str(e)[:80]}", level="warn")

        await asyncio.sleep(0.6)

        # ── WAY 3: CSS selector + force-click ──
        try:
            btn = hcaptcha.locator("#menu-info").first
            await btn.wait_for(state="visible", timeout=3000)
            await btn.click(force=True, timeout=3000)
            log("[Accessibility] Force-clicked #menu-info (way 3)")
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            log(f"[Accessibility] way 3 (force-click) failed: {str(e)[:80]}", level="warn")

        await asyncio.sleep(0.6)

        # ── WAY 4: Dispatch event on any matching element ──
        try:
            await hcaptcha.locator("#menu-info").first.dispatch_event("click")
            log("[Accessibility] Dispatched click event (way 4)")
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            log(f"[Accessibility] way 4 (dispatch) failed: {str(e)[:80]}", level="warn")

        return False

    async def _click_accessibility_option(hcaptcha) -> bool:
        """Select 'Accessibility Challenge' from the already-open menu.
        Menu items in hCaptcha have child spans — don't skip containers.
        Just find any visible element with short text matching the label."""
        deadline = time.time() + 8
        poll = 0
        while time.time() < deadline:
            poll += 1
            # JS: find ANY visible element whose trimmed text is the menu label.
            # hCaptcha menu items look like <div role="menuitem"><span>The Label</span></div>
            # so textContent works but children.length > 0 would wrongly skip them.
            try:
                clicked = await _challenge_js("""() => {
                    // Match any element with short text matching the label
                    const all = document.querySelectorAll('*');
                    for (const el of all) {
                        if (el.offsetParent === null) continue;
                        const t = (el.textContent || '').trim();
                        if (!t || t.length > 60 || t.length < 8) continue;
                        if (/^Accessibility Challenge$/i.test(t)) {
                            el.scrollIntoView({block: 'center'});
                            el.click();
                            return t;
                        }
                    }
                    // Fallback: partial match on short text
                    for (const el of all) {
                        if (el.offsetParent === null) continue;
                        const t = (el.textContent || '').trim();
                        if (t.length > 60 || t.length < 5) continue;
                        if (/accessibility.*challenge/i.test(t)) {
                            el.scrollIntoView({block: 'center'});
                            el.click();
                            return t;
                        }
                    }
                    return null;
                }""")
                if clicked:
                    log(f"[Accessibility] JS-clicked menu item: '{clicked}' (poll {poll})")
                    return True
            except Exception:
                pass
            # Playwright fallback: text match
            try:
                loc = hcaptcha.get_by_text("Accessibility Challenge", exact=False).first
                await loc.wait_for(state="visible", timeout=1500)
                await loc.click(timeout=2000)
                log(f"[Accessibility] Clicked via text locator (poll {poll})")
                return True
            except Exception:
                pass
            # Playwright fallback: role match
            for role in ("link", "button", "menuitem"):
                try:
                    loc = hcaptcha.get_by_role(role, name="Accessibility Challenge").first
                    await loc.wait_for(state="visible", timeout=1500)
                    await loc.click(timeout=2000)
                    log(f"[Accessibility] Clicked via role={role} (poll {poll})")
                    return True
                except Exception:
                    pass
            await asyncio.sleep(0.7)
        return False

    async def _open_accessibility_challenge(hcaptcha) -> bool:
        """Open accessibility challenge with detection at each step.
        Click one 3-dots method → wait 5s for menu → click option → detect input.
        If any step fails, retry with next method."""

        # ── Step A: Click 3-dots (one method at a time, detect menu open) ──
        menu_opened = False

        # WAY 1: JS click
        try:
            js_result = await _challenge_js("""() => {
                const btn = document.querySelector('#menu-info')
                         || document.querySelector('[aria-label*="About hCaptcha"]')
                         || document.querySelector('[aria-label*="Extra menu"]')
                         || document.querySelector('.display-menu-btn');
                if (btn && btn.offsetParent !== null) {
                    btn.scrollIntoView({block: 'center'});
                    btn.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
                    btn.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
                    btn.dispatchEvent(new MouseEvent('click', {bubbles:true}));
                    return 'ok';
                }
                return null;
            }""")
            if js_result:
                log("[Accessibility] Clicked 3-dots via JS (way 1)")
                for _ in range(6):  # 3 seconds
                    if await _menu_visible(hcaptcha):
                        menu_opened = True
                        log("[Accessibility] Menu opened (way 1)")
                        break
                    await asyncio.sleep(0.5)
        except Exception as e:
            log(f"[Accessibility] way 1 (JS) failed: {str(e)[:60]}", level="warn")

        if not menu_opened:
            # WAY 2: Playwright role click
            try:
                for label in ("About hCaptcha & Accessibility Options", "Extra menu", "More options", "Menu"):
                    try:
                        btn = hcaptcha.get_by_role("button", name=label).first
                        await btn.wait_for(state="visible", timeout=3000)
                        await btn.click(timeout=2000)
                        log(f"[Accessibility] Clicked 3-dots via role '{label}' (way 2)")
                        for _ in range(6):
                            if await _menu_visible(hcaptcha):
                                menu_opened = True
                                break
                            await asyncio.sleep(0.5)
                        if menu_opened:
                            break
                    except Exception:
                        continue
            except Exception as e:
                log(f"[Accessibility] way 2 (role) failed: {str(e)[:60]}", level="warn")

        if not menu_opened:
            # WAY 3: CSS force-click
            try:
                btn = hcaptcha.locator("#menu-info").first
                await btn.wait_for(state="visible", timeout=3000)
                await btn.click(force=True, timeout=3000)
                log("[Accessibility] Force-clicked #menu-info (way 3)")
                for _ in range(6):
                    if await _menu_visible(hcaptcha):
                        menu_opened = True
                        break
                    await asyncio.sleep(0.5)
            except Exception as e:
                log(f"[Accessibility] way 3 (force) failed: {str(e)[:60]}", level="warn")

        if not menu_opened:
            # WAY 4: dispatch event
            try:
                await hcaptcha.locator("#menu-info").first.dispatch_event("click")
                log("[Accessibility] Dispatched click (way 4)")
                for _ in range(6):
                    if await _menu_visible(hcaptcha):
                        menu_opened = True
                        break
                    await asyncio.sleep(0.5)
            except Exception as e:
                log(f"[Accessibility] way 4 (dispatch) failed: {str(e)[:60]}", level="warn")

        if not menu_opened:
            log("[Accessibility] Menu never opened after all 4 methods", level="warn")
            return False

        # ── Step B: Menu is open — click Accessibility Challenge option ──
        await asyncio.sleep(0.5)  # let animation finish
        clicked_opt = await _click_accessibility_option(hcaptcha)
        if not clicked_opt:
            log("[Accessibility] Could not click accessibility option", level="warn")
            return False

        # ── Step C: Wait for the challenge to render, then return ──
        # Poll for the challenge INPUT instead of a blind 10s wait: the
        # question is solvable as soon as its input is interactive, which
        # is usually 2-4s, not 10. Hard cap 10s for slow loads.
        log("[Accessibility] Accessibility option clicked — polling for challenge input (max 10s)")
        poll_js = (
            "() => {"
            "const inputs = document.querySelectorAll('input, textarea');"
            "for (const inp of inputs) {"
            "if (inp.type !== 'hidden' && inp.offsetParent !== null) return 'input:' + inp.tagName;"
            "}"
            "return null;"
            "}"
        )
        for _ci in range(12):  # 12 x 0.5s = 6s max
            try:
                _r = await _challenge_js(poll_js)
                if _r and 'input:' in str(_r):
                    log("[Accessibility] Challenge input interactive — proceeding")
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
        log("[Accessibility] Challenge wait complete (10s max) — proceeding to screenshot + AI solve")
        return True

    def _find_options_line(best_source: str, all_texts, best_line: str) -> str:
        """For 'pick the one that represents an X' challenges the candidate words
        are rendered on their OWN line (e.g. 'oar, glass, piglet'). Find a short
        comma-separated list of words near the question."""
        sources = ([t for s, t in all_texts if s == best_source]
                   + [t for s, t in all_texts if s != best_source])
        for text in sources:
            for line in re.split(r'[\n|]', text):
                line = line.strip()
                if not line or len(line) > 60:
                    continue
                if line in best_line or best_line in line:
                    continue
                if not re.match(r"^[a-zA-Z][a-zA-Z ,-]{3,55}$", line):
                    continue
                parts = [p.strip() for p in line.split(',')]
                if 2 <= len(parts) <= 4 and all(2 <= len(p) <= 18 for p in parts):
                    return line
        return ''

    async def _read_question_text() -> str:
        """EXTREME search: scan page.innerText, EVERY frame innerText,
        and the hCaptcha frame body for question text patterns."""
        all_texts = []

        # Method 0: JS scan for img[alt] and aria-* — accessibility challenges
        # render the question as an <img alt="You have a jar with 9 coins...">
        # which inner_text() does NOT capture!
        async def _scan_js(source, js_func):
            try:
                val = await js_func()
                if val and str(val).strip() and len(str(val).strip()) > 3:
                    return str(val).strip()
            except Exception:
                pass
            return None

        # JS that captures alt/aria text (the actual question for accessibility)
        aria_js = """() => {
            const parts = [];
            for (const el of document.querySelectorAll('img[alt], [aria-label], [aria-describedby]')) {
                if (el.offsetParent === null && el.tagName !== 'IMG') continue;
                const t = (el.getAttribute('alt') || el.getAttribute('aria-label') || '').trim();
                if (t && t.length > 8 && t.length < 600) parts.push(t);
            }
            // Also grab ALL visible text (includes headings, paragraphs)
            const bodyText = document.body ? (document.body.innerText || '') : '';
            if (bodyText.trim()) parts.push(bodyText.trim());
            return parts.join('\n');
        }"""

        # Run aria scan on the hCaptcha frame first
        for frame_source in [lambda: hcaptcha.locator("body").evaluate(aria_js),
                             lambda: page.evaluate(aria_js)]:
            val = await _scan_js("aria-js", frame_source)
            if val:
                all_texts.insert(0, ("aria-alt", val))
                break

        # Method 1: hCaptcha frame body innerText
        try:
            t = await hcaptcha.locator("body").inner_text()
            if t and len(t.strip()) > 5:
                all_texts.append(("hcaptcha-body", t.strip()))
        except Exception:
            pass

        # Method 2: page.evaluate document.body.innerText
        try:
            t = await page.evaluate('() => document.body ? document.body.innerText : ""')
            if t and len(t.strip()) > 5:
                all_texts.append(("page-body", t.strip()))
        except Exception:
            pass

        # Method 3: iterate ALL frames and read innerText
        try:
            for i, frame in enumerate(page.frames):
                try:
                    t = await frame.evaluate('() => document.body ? document.body.innerText : ""')
                    if t and len(t.strip()) > 5:
                        all_texts.append((f"frame-{i}", t.strip()))
                except Exception:
                    continue
        except Exception:
            pass

        # Method 4: page.locator("body").inner_text()
        try:
            t = await page.locator("body").inner_text()
            if t and len(t.strip()) > 5:
                all_texts.append(("page-locator", t.strip()))
        except Exception:
            pass

        # ── Now scan all collected texts and SCORE lines ──
        # The instruction line ("Read and answer with 1 word") matches weak
        # keywords too, so we must pick the line with the MOST question
        # keywords, not the first line with any keyword.
        best_line = None
        best_score = 0
        best_source = None

        for source, text in all_texts:
            lines = text.split(chr(10))
            for line in lines:
                line = line.strip()
                if len(line) < 8 or len(line) > 500:
                    continue
                score = 0
                # STRONG keywords (the actual question uses these):
                # jar/coins math
                if re.search(r'\bjar\b|\bcoins?\b|\bhow many\b|\baltogether\b|\bin all\b', line, re.IGNORECASE):
                    score += 4
                if re.search(r'\badd\b|\bput\b|\btotal\b|\bhas\b|\bstart with\b', line, re.IGNORECASE):
                    score += 2
                # word puzzles
                if re.search(r'\bremove\b|\bdelet\w*\b|\bdrop\b|\bstrip\b', line, re.IGNORECASE):
                    score += 4
                if re.search(r'\bfirst\b', line, re.IGNORECASE):
                    score += 3
                if re.search(r'\blast\b', line, re.IGNORECASE):
                    score += 3
                if re.search(r'\bletter\w*\b|\bcharacter\w*\b', line, re.IGNORECASE):
                    score += 3
                if re.search(r'\breverse\b|\bbackward\w*\b', line, re.IGNORECASE):
                    score += 3
                if re.search(r'\bword\b', line, re.IGNORECASE):
                    score += 2
                # Animal challenge: "pick the word that is an animal"
                if re.search(r'\banimal\b|\bcreature\b|\bbeast\b|\bliving\b.*\bthing\b|\bwhich\b.*\banimal\b', line, re.IGNORECASE):
                    score += 5

                # Country challenge: "choose the country" / "pick the country"
                if re.search(r'\bcountry\b|\bcountries\b|\bnation\b|\bnations\b', line, re.IGNORECASE):
                    score += 5
                # Knowledge questions (rooms, colors, counting, calendar...)
                if re.search(r'\broom\b|\bsink\b|\bkitchen\b|\bbedroom\b|\bbathroom\b', line, re.IGNORECASE):
                    score += 4
                if re.search(r'what (?:color|colour|room)|which (?:room|color)|color of|colour of', line, re.IGNORECASE):
                    score += 4
                if re.search(r'\blegs\b|\bwheels\b|\bhow many\b|\bhow much\b', line, re.IGNORECASE):
                    score += 3
                if re.search(r'\bmonth\b|\bseason\b|\bday\b|\bweek\b|\byear\b', line, re.IGNORECASE):
                    score += 2
                if re.search(r'\bsink\b|\bdishes\b|\bmoos?\b|\bquacks?\b|\bmeows?\b|\bbarks?\b|\bneighs?\b', line, re.IGNORECASE):
                    score += 3
                if re.search(r'\bcapital\b|\bfruit\b|\bvegetable\b|\binsect\b|\bwhich of these\b', line, re.IGNORECASE):
                    score += 3
                if re.search(r'\bused to\b|\buse .* to\b|\bwhat do you\b', line, re.IGNORECASE):
                    score += 3
                # numbers reinforce a real math question
                if re.search(r'\b\d+\b', line):
                    score += 2
                # Penalize pure instruction lines
                if re.search(r'^\s*(?:read|answer|respond|type|please|question)\b', line, re.IGNORECASE):
                    score -= 2
                if re.search(r'read and answer|answer with|respond with|single word', line, re.IGNORECASE):
                    score -= 4
                # Heavily penalize signup-form / non-captcha UI text
                # (happens when the accessibility iframe fails to load and
                #  the solver reads the parent page's signup form instead)
                if re.search(r'\b(?:email\*?|password\*?|username\*?|display\s+name|create\s+(?:an\s+)?account|sign\s*(?:up|in)|log\s*in|this is how others see you)\b', line, re.IGNORECASE):
                    score -= 15
                if re.search(r'\b(?:available|nice!|special characters|emoji)\b', line, re.IGNORECASE):
                    score -= 8

                if score > best_score:
                    best_score = score
                    best_line = line
                    best_source = source

        if best_line and best_score >= 3:
            # Picker questions ("pick the one that represents an animal") render
            # the candidate words on a SEPARATE line ("oar, glass, piglet").
            # Append that options line so the solver sees the candidates.
            if re.search(r'pick the one|pick the word|words below|represents|which of these|which one|choose the|pick the country', best_line, re.IGNORECASE):
                extra = _find_options_line(best_source, all_texts, best_line)
                if extra:
                    best_line = best_line + ' : ' + extra
            log(f"[Accessibility] Scored question ({best_score}) from {best_source}: '{best_line[:160]}'")
            return best_line

        # Priority fallback: concatenated text from any source
        # BUT reject text that looks like a signup form or generic page UI
        # (not a captcha question). This prevents infinite-loop on pages
        # where the accessibility iframe fails to load a real challenge.
        _FORM_SIGNALS = re.compile(
            r'(?:email\*?|password\*?|username\*?|display\s+name|'
            r'create\s+(?:an\s+)?account|sign\s*(?:up|in)|log\s*in|'
            r'this is how others see you|nice!|special characters)',
            re.IGNORECASE
        )
        for source, text in all_texts:
            if len(text) > 10:
                if _FORM_SIGNALS.search(text):
                    log(f"[Accessibility] Raw text from {source} looks like a signup form — skipping",
                        level="warn")
                    continue
                log(f"[Accessibility] No scored question — returning raw text from {source}")
                return text[:500]

        return ''

    def _find_target_word(text: str) -> Optional[str]:
        """Given a word puzzle question, extract the target word.
        Looks for quoted words, ALL-CAPS, or the longest word in the question."""
        # Strategy 1: Word in quotes
        m = re.search('["“”](\w{3,})["“”]', text)
        if m:
            return m.group(1)
        # Strategy 2: Word after "from the word X" / "the word X" / "word X" / "word is X"
        m = re.search(r'(?:from\s+)?(?:the\s+)?word\s+(?:is\s+|:\s+|of\s+|mayor\s*)?(\w{3,})',
                      text, re.IGNORECASE)
        if m:
            candidate = m.group(1)
            # 'of' could be caught as the word if the "word of" form is used;
            # only accept real content words
            if candidate.lower() not in ('of', 'the', 'and', 'is', 'a', 'an'):
                return candidate
        # Strategy 3: ALL-CAPS word (often the target in these puzzles)
        caps = re.findall(r'\b([A-Z]{3,})\b', text)
        if caps:
            return caps[0]
        # Strategy 4: Find the longest word (likely the target)
        words = re.findall(r'\b([a-zA-Z]{3,})\b', text)
        if words:
            # Filter out common question words (incl. the removal verbs and
            # puzzle instructions so 'remaining'/'backward' can't win)
            skip = {'what','the','and','remove','removing','removed','delete',
                    'deleting','deleted','drop','dropped','strip','take','taken',
                    'erase','erased','first','last','letter','letters','character',
                    'characters','write','writing','backwards','backward',
                    'reverse','reversed','remaining','type','this','that',
                    'with','your','after','from','word','them','it','into',
                    'before','then','which','given','named','called','please',
                    'answer','following','question','off','out','of','the','and'}
            candidates = [w for w in words if w.lower() not in skip]
            if candidates:
                return max(candidates, key=len)
        return None

    def _solve_text_question(text: str) -> Optional[str]:
        """Try to answer a text question locally without Ollama.
        Handles: math chains (5+8+7), word puzzles (remove first/last letter
        and reverse), simple arithmetic, etc."""
        t = text.strip().lower()
        orig = text.strip()

        # ── JAR SOLVER (simple & bulletproof) ──
        # If the word "jar" appears anywhere in the question, it's a jar
        # question. Take ALL digits, add them together, that's the answer.
        # e.g. "You have a jar with 6 coins. On Tuesday, you put 3 coins into
        #       the jar. How many coins are in the jar?" → 6+3 = 9
        if re.search(r'\bjar\b', t, re.IGNORECASE):
            all_nums = re.findall(r'\b(\d+)\b', orig)
            if all_nums:
                total = sum(int(n) for n in all_nums)
                log(f"[Accessibility] Jar solver: {'+'.join(all_nums)} = {total}")
                return str(total)

        # ── COIN word problems (no 'jar' word): sum all numbers ──
        # "There are 3 coins in the box. On Sunday you add 6 coins..." → 3+6+8
        if re.search(r'\bcoins?\b|\badd\b|\bput\b|\baltogether\b|\bin\s+all\b', t, re.IGNORECASE):
            all_nums = re.findall(r'\b(\d+)\b', orig)
            if all_nums:
                total = sum(int(n) for n in all_nums)
                log(f"[Accessibility] Coin sum: {'+'.join(all_nums)} = {total}")
                return str(total)

        # ── MATH: robust chain detection ──
        # Match expressions like "5 + 8 + 7", "12 × 3 ÷ 2", "10 - 2 - 3"
        math_re = re.compile(
            r'(?:(?:what\s+is|calculate|compute|solve|evaluate|find)[:\s]*)?'
            r'(-?\d+)\s*([+\-×xX*÷/])\s*(-?\d+)(\s*([+\-×xX*÷/])\s*(-?\d+))*',
            re.IGNORECASE
        )
        m = math_re.search(orig)
        if m:
            # Reconstruct the full chain from the match
            full_expr = m.group(0)
            # Clean up any leading text like "what is "
            full_expr = re.sub(r'^[a-z\s]+[:\s]*', '', full_expr, flags=re.IGNORECASE)
            ans = _eval_arithmetic_chain(full_expr)
            if ans is not None:
                log(f"[Accessibility] Math chain: '{full_expr}' = {ans}")
                return ans

        # ── WORD PUZZLES: "remove/drop first and last letter, write backwards" ──
        # Loose detection: any of remove/drop/delete/strip/take + first + last
        # + letter/character. The "write it backwards/reverse" tail varies, so
        # it is optional. Target word = LAST word of the question sentence.
        # TIGHTER: require that "first" AND "last" are followed by
        # "letter"/"character" within a few words. Prevents matching
        # "remove the first item from the list and the last" (no letter).
        # Detection is deliberately LOOSE: any removal verb (removing/remove/
        # delete/drop/strip/take/eliminate/erase, any tense) + first & last +
        # letter/character(s), in EITHER word order. hCaptcha's common
        # phrasing is 'After removing the first and last letters write the
        # remaining letters backward from the word adam' - the old regex
        # demanded 'first letter' adjacency and \bremove\b (which misses
        # 'removing'), so this exact question fell through to the LLM and
        # timed out for 88s. Now solved locally in milliseconds.
        _wverb = r'(?:remov(?:e|es|ed|ing)?|delet(?:e|es|ed|ing)?|drop(?:ped|ping)?|strip(?:ped|ping)?|take(?:n|ing)?|eliminat(?:e|es|ed|ing)?|erase(?:d|s)?)'
        word_pat = re.compile(
            r'(?:' + _wverb + r'\s+(?:out\s+)?(?:the\s+)?'
            r'(?:first|1st)(?:\s*(?:and|&)\s*(?:the\s+)?(?:last|2nd))?\s+'
            r'(?:letter|character|char)s?'
            r'|(?:first|1st)(?:\s*(?:and|&)\s*(?:the\s+)?(?:last|2nd))?\s+'
            r'(?:letter|character|char)s?.*' + _wverb + r')',
            re.IGNORECASE
        )
        if word_pat.search(orig) or (re.search(_wverb, t)
                                     and re.search(r'\b(?:first|1st)\b', t)
                                     and re.search(r'\b(?:last|2nd)\b', t)
                                     and re.search(r'\b(?:letter|character|char)s?\b', t)):
            # Strategy A: quoted / "of the word X" / "word is X" / ALL-CAPS
            # (most reliable — hCaptcha always names the word explicitly)
            word = _find_target_word(orig)
            # Strategy B: LAST word of the sentence as fallback
            if not word or len(word) <= 2:
                words = re.findall(r'[A-Za-z]{2,}', orig)
                if words:
                    skip_tail = {'backwards', 'backward', 'reverse', 'reversed',
                                 'direction', 'remaining', 'them', 'it', 'the',
                                 'and', 'write', 'spell', 'type', 'put', 'word',
                                 'letter', 'letters', 'order', 'answer', 'please',
                                 'from', 'with', 'into', 'your', 'in'}
                    for w in reversed(words):
                        if w.lower() in skip_tail:
                            continue
                        word = w
                        break
            if word and len(word) > 2:
                # Remove first and last letter, then reverse
                result = word[1:-1][::-1]
                if result:
                    log(f"[Accessibility] Word puzzle: '{word}' -> remove '{word[0]}'+'{word[-1]}' -> '{word[1:-1]}' -> reverse -> '{result}'")
                    return result

        # ── SIMPLE ARITHMETIC (single operation, e.g. "3 + 5") ──
        simple_pat = re.compile(r'(-?\d+)\s*([+\-×xX*÷/])\s*(-?\d+)')
        sm = simple_pat.search(t)
        if sm:
            ans = _eval_arithmetic_chain(sm.group(0))
            if ans is not None:
                log(f"[Accessibility] Simple math: '{sm.group(0)}' = {ans}")
                return ans

        # ── ANIMAL WORD puzzle: given 3 words, pick the animal ──
        # "seal, trash, bucket" → seal is the animal
        animal_pat = re.search(
            r'(?:animal|creature|beast|living\s+thing|which\s+one\s+is)',
            t, re.IGNORECASE
        )
        if animal_pat:
            # Extract all words from the text (3+ letters)
            words = re.findall(r'\b([a-zA-Z]{3,})\b', orig)
            # Generic category words ("animal", "creature", "one"...) are NOT
            # answers — the real candidates are the option words. Exclude them.
            generic = {'animal', 'creature', 'beast', 'living', 'thing', 'which',
                       'one', 'pick', 'the', 'that', 'from', 'words', 'below',
                       'represents', 'an', 'and', 'are', 'is', 'of', 'these',
                       'them', 'with', 'you', 'your', 'following', 'any'}
            candidates = [w for w in words if w.lower() in ANIMAL_WORDS and w.lower() not in generic]
            if candidates:
                log(f"[Accessibility] Animal challenge: candidates={candidates} from {words}")
                return candidates[0]
            # Broader: normalize and check against ANIMAL_WORDS
            for w in words:
                if w.lower() in ANIMAL_WORDS and w.lower() not in generic:
                    return w

        # COUNTRY WORD puzzle: given 3 words, pick the country
        # "Choose the country: France, potato, chair" -> France is the country
        country_pat = re.search(
            r'\b(?:country|countries|nation|nations)\b',
            t, re.IGNORECASE
        )
        if country_pat:
            # Multi-word countries first (full phrase match)
            for phrase in sorted(COUNTRY_PHRASES, key=len, reverse=True):
                if re.search(r'\b' + re.escape(phrase) + r'\b', t):
                    log(f"[Accessibility] Country challenge: multi-word '{phrase}'")
                    return phrase
            # Single-word countries (the "3 words" option-list case)
            words = re.findall(r'\b([a-zA-Z]{3,})\b', orig)
            generic = {'country', 'countries', 'nation', 'nations', 'choose',
                       'pick', 'select', 'which', 'one', 'word', 'words', 'the',
                       'that', 'from', 'below', 'represents', 'an', 'a', 'and',
                       'are', 'is', 'not', 'of', 'these', 'them', 'with', 'you',
                       'your', 'following', 'any', 'answer', 'question'}
            negated = bool(re.search(r'\bnot\b', t))
            if negated:
                # "Which of these is NOT a country" -> pick a non-country word
                for w in words:
                    lw = w.lower()
                    if lw not in generic and lw not in COUNTRY_WORDS:
                        log(f"[Accessibility] Country challenge (NOT): {w} from {words}")
                        return w
            else:
                candidates = [w for w in words if w.lower() in COUNTRY_WORDS and w.lower() not in generic]
                if candidates:
                    log(f"[Accessibility] Country challenge: candidates={candidates} from {words}")
                    return candidates[0]

        # ── KNOWLEDGE QUESTIONS (rooms, colors, animal sounds, counting...) ──
        # Runs before number extraction so "how many legs" etc. hit the KB.
        knowledge_ans = _solve_knowledge_question(text)
        if knowledge_ans is not None:
            log(f"[Accessibility] Knowledge answer: {knowledge_ans}")
            return knowledge_ans

        # ── Semantic fallback: topic keywords match any phrasing ──
        semantic_ans = _solve_semantic(text)
        if semantic_ans is not None:
            log(f"[Accessibility] Semantic answer: {semantic_ans}")
            return semantic_ans

        # ── PURE NUMBER extraction (e.g. "type the number 42") ──
        num_pat = re.search(r'(?:number|digit|num)\s+[iof]*\s*(\d+)', t)
        if num_pat:
            return num_pat.group(1)

        # ── Just a number? ──
        lone_num = re.search(r'^\s*(\d+)\s*$', t)
        if lone_num:
            return lone_num.group(1)

        # ── FIRST / LAST LETTER: "What is the first letter of X?" ──
        fl_letter = re.search(
            r'(?:what|which).*(?:first|1st)\s+letter\s+(?:of|in)\s+(?:the\s+)?(?:word\s+)?["\']?(\w{2,})',
            t, re.IGNORECASE
        )
        if fl_letter:
            word = fl_letter.group(1)
            if len(word) > 1:
                log(f"[Accessibility] First letter of '{word}' = '{word[0]}'")
                return word[0].lower()

        ll_letter = re.search(
            r'(?:what|which).*(?:last)\s+letter\s+(?:of|in)\s+(?:the\s+)?(?:word\s+)?["\']?(\w{2,})',
            t, re.IGNORECASE
        )
        if ll_letter:
            word = ll_letter.group(1)
            if len(word) > 1:
                log(f"[Accessibility] Last letter of '{word}' = '{word[-1]}'")
                return word[-1].lower()

        # ── LETTER COUNT: "How many letters in the word X?" ──
        letter_count = re.search(
            r'how many letters\s+(?:in|are in)\s+(?:the\s+)?(?:word\s+)?["\']?(\w{2,})',
            t, re.IGNORECASE
        )
        if letter_count:
            word = letter_count.group(1)
            count = len(word)
            log(f"[Accessibility] Letter count of '{word}' = {count}")
            return str(count)

        # ── LETTER AFTER/BEFORE: "What letter comes after X?" ──
        letter_after = re.search(
            r'(?:what|which)\s+letter\s+comes?\s+after\s+["\']?(\w)["\']?',
            t, re.IGNORECASE
        )
        if letter_after:
            ch = letter_after.group(1).lower()
            if 'a' <= ch < 'z':
                nxt = chr(ord(ch) + 1)
                log(f"[Accessibility] Letter after '{ch}' = '{nxt}'")
                return nxt

        letter_before = re.search(
            r'(?:what|which)\s+letter\s+comes?\s+before\s+["\']?(\w)["\']?',
            t, re.IGNORECASE
        )
        if letter_before:
            ch = letter_before.group(1).lower()
            if 'a' < ch <= 'z':
                prev = chr(ord(ch) - 1)
                log(f"[Accessibility] Letter before '{ch}' = '{prev}'")
                return prev

        # ── NUMBER AFTER/BEFORE: "What number comes after X?" ──
        num_after = re.search(
            r'(?:what|which)\s+(?:number|digit)\s+comes?\s+after\s+(\d+)',
            t, re.IGNORECASE
        )
        if num_after:
            n = int(num_after.group(1))
            log(f"[Accessibility] Number after {n} = {n + 1}")
            return str(n + 1)

        num_before = re.search(
            r'(?:what|which)\s+(?:number|digit)\s+comes?\s+before\s+(\d+)',
            t, re.IGNORECASE
        )
        if num_before:
            n = int(num_before.group(1))
            log(f"[Accessibility] Number before {n} = {n - 1}")
            return str(n - 1)

        # ── PORTMANTEAU / COMBINATION: "What meal is brunch a combination of?" ──
        # hCaptcha often asks what two things combine to form a word.
        # We match the target word and return its first component.
        combo = re.search(
            r'(?:what|which)\s+(?:meal|word|term|thing)\s+(?:is|are)\s+(\w{3,})\s+(?:a\s+)?combination\s+of',
            t, re.IGNORECASE
        )
        if not combo:
            combo = re.search(
                r'(\w{3,})\s+(?:is|are)\s+a\s+combination\s+of',
                t, re.IGNORECASE
            )
        if combo:
            target = combo.group(1).lower()
            portmanteaus = {
                'brunch': 'breakfast', 'smog': 'smoke',
                'motel': 'motor', 'spork': 'spoon',
                'cyborg': 'cybernetic', 'email': 'electronic',
                'pixel': 'picture', 'modem': 'modulator',
                'blog': 'web', 'vlog': 'video',
                'hangry': 'hungry', 'chillax': 'chill',
                'frenemy': 'friend', 'bromance': 'brother',
                'mocktail': 'mock', 'staycation': 'stay',
                'glamping': 'glamorous', 'workaholic': 'work',
                'shopaholic': 'shop', 'chocoholic': 'chocolate',
                'brinner': 'breakfast', 'linner': 'lunch',
                'moped': 'motor', 'transponder': 'transmitter',
                'telethon': 'telephone', 'webinar': 'web',
                'emoticon': 'emotion', 'malware': 'malicious',
                'spam': 'spiced', 'situationship': 'situation',
            }
            if target in portmanteaus:
                ans = portmanteaus[target]
                log(f"[Accessibility] Portmanteau: '{target}' = '{ans}' + ...")
                return ans

        # ── RIDDLES: "What has keys but can't open locks?" ──
        # Returns the single-word answer to common riddles.
        riddle = re.search(
            r'what\s+has\s+(.+?)\s+but\s+(?:can\s*["\']?t|cannot)\s+(.+)',
            t, re.IGNORECASE
        )
        if riddle:
            # Check against known riddles
            riddles = {
                ('keys', 'open'): 'piano',
                ('hands', 'clap'): 'clock',
                ('face', 'smile'): 'clock',
                ('head', 'wear'): 'coin',
                ('teeth', 'bite'): 'comb',
                ('needle', 'sew'): 'pine tree',
                ('bed', 'sleep'): 'river',
                ('legs', 'walk'): 'table',
                ('wings', 'fly'): 'airplane',
                ('eyes', 'see'): 'needle',
                ('bank', 'money'): 'river',
                ('branches', 'tree'): 'bank',
            }
            has = riddle.group(1).lower().strip()
            cant = riddle.group(2).lower().strip()
            for (k, c), ans in riddles.items():
                if k in has and c in cant:
                    log(f"[Accessibility] Riddle: '{has} but can't {cant}' -> {ans}")
                    return ans

        # ── SYNONYMS: "What is another word for X?" ──
        syn = re.search(
            r'(?:what|which)\s+(?:is|are)\s+(?:another|a different|a)\s+word\s+for\s+(\w{3,})',
            t, re.IGNORECASE
        )
        if syn:
            target = syn.group(1).lower()
            synonyms = {
                'angry': 'mad', 'mad': 'angry',
                'happy': 'glad', 'glad': 'happy',
                'sad': 'unhappy', 'big': 'large',
                'small': 'little', 'fast': 'quick',
                'quick': 'fast', 'smart': 'clever',
                'clever': 'smart', 'brave': 'courageous',
                'pretty': 'beautiful', 'ugly': 'hideous',
                'rich': 'wealthy', 'poor': 'destitute',
                'scared': 'afraid', 'tired': 'exhausted',
                'begin': 'start', 'end': 'finish',
                'help': 'assist', 'buy': 'purchase',
                'talk': 'speak', 'close': 'shut',
                'error': 'mistake', 'job': 'occupation',
                'kid': 'child', 'dad': 'father',
                'mom': 'mother', 'pal': 'friend',
            }
            if target in synonyms:
                ans = synonyms[target]
                log(f"[Accessibility] Synonym: '{target}' -> '{ans}'")
                return ans

        # ── HOMOPHONES: "Which word sounds like X?" ──
        homo = re.search(
            r'(?:what|which)\s+word\s+sounds?\s+(?:like|same as)\s+(\w{2,})',
            t, re.IGNORECASE
        )
        if homo:
            target = homo.group(1).lower()
            homophones = {
                'there': 'their', 'their': 'there',
                'to': 'two', 'two': 'to',
                'sea': 'see', 'see': 'sea',
                'here': 'hear', 'hear': 'here',
                'sun': 'son', 'son': 'sun',
                'flower': 'flour', 'flour': 'flower',
                'night': 'knight', 'knight': 'night',
                'write': 'right', 'right': 'write',
                'peace': 'piece', 'piece': 'peace',
                'bear': 'bare', 'bare': 'bear',
                'dear': 'deer', 'deer': 'dear',
                'mail': 'male', 'male': 'mail',
                'sale': 'sail', 'sail': 'sale',
                'meet': 'meat', 'meat': 'meet',
                'pair': 'pear', 'pear': 'pair',
                'hair': 'hare', 'hare': 'hair',
            }
            if target in homophones:
                ans = homophones[target]
                log(f"[Accessibility] Homophone: '{target}' -> '{ans}'")
                return ans

        # ── "WHAT DO YOU USE TO..." OBJECT-FUNCTION SOLVER ──
        # hCaptcha frequently asks "What do you use to X?" or "What do you use for X?"
        # We maintain a massive table of function -> object mappings.
        use_to = re.search(
            r'(?:what|which)\s+(?:do\s+you\s+)?(?:use|object|tool|item|thing)\s+(?:to|for)\s+(.+?)(?:\s*[?.!]|$)',
            t, re.IGNORECASE
        )
        if use_to:
            func = use_to.group(1).lower().strip()
            # Strip trailing punctuation and filler words
            func = re.sub(r'\s+(?:a|an|the|with|using|from|of|in|on|at|by)\s*$', '', func).strip()
            # Massive lookup table: function description -> object
            obj_table = {
                # Office / paper / writing
                'fasten papers together': 'stapler', 'fasten papers with a metal wire': 'stapler',
                'fasten papers': 'stapler', 'staple papers': 'stapler',
                'remove staples': 'staple remover', 'take out staples': 'staple remover',
                'cut paper': 'scissors', 'cut paper straight': 'paper cutter',
                'write on paper': 'pen', 'write with ink': 'pen',
                'erase pencil marks': 'eraser', 'remove pencil marks': 'eraser',
                'rub out pencil': 'eraser', 'correct mistakes in writing': 'eraser',
                'sharpen a pencil': 'pencil sharpener', 'make pencil sharp': 'pencil sharpener',
                'highlight text': 'highlighter', 'mark important text': 'highlighter',
                'measure length': 'ruler', 'draw straight lines': 'ruler',
                'measure angles': 'protractor', 'draw circles': 'compass',
                'stick papers together': 'glue', 'attach papers': 'glue',
                'hold papers together temporarily': 'paper clip', 'clip papers': 'paper clip',
                'organize papers': 'binder', 'hold papers in a folder': 'folder',
                'punch holes in paper': 'hole punch', 'make holes in paper': 'hole punch',
                'store documents': 'filing cabinet', 'file papers': 'filing cabinet',
                'write notes': 'notebook', 'take notes': 'notebook',
                'carry books': 'backpack', 'carry school supplies': 'backpack',
                'calculate numbers': 'calculator', 'do math': 'calculator',
                'print documents': 'printer', 'make copies': 'copier',
                'scan documents': 'scanner', 'send a fax': 'fax machine',
                'type on a computer': 'keyboard', 'point and click on screen': 'mouse',
                'see things on a computer': 'monitor', 'display computer output': 'monitor',
                'store computer files': 'hard drive', 'save data': 'hard drive',
                'protect computer from viruses': 'antivirus', 'connect to internet': 'modem',
                # Kitchen / cooking / food
                'cut food': 'knife', 'cut meat': 'knife', 'slice bread': 'knife',
                'eat soup': 'spoon', 'eat cereal': 'spoon', 'stir food': 'spoon',
                'eat salad': 'fork', 'pick up food': 'fork',
                'drink water': 'cup', 'drink hot beverages': 'mug',
                'boil water': 'kettle', 'heat water': 'kettle',
                'cook food quickly': 'microwave', 'reheat food': 'microwave',
                'bake a cake': 'oven', 'roast food': 'oven',
                'fry food': 'pan', 'cook on stove': 'pot',
                'toast bread': 'toaster', 'make toast': 'toaster',
                'blend ingredients': 'blender', 'mix smoothies': 'blender',
                'chop vegetables': 'knife', 'peel vegetables': 'peeler',
                'grate cheese': 'grater', 'shred food': 'grater',
                'open cans': 'can opener', 'open bottles': 'bottle opener',
                'open wine': 'corkscrew', 'crack nuts': 'nutcracker',
                'measure ingredients': 'measuring cup', 'weigh food': 'scale',
                'keep food cold': 'refrigerator', 'freeze food': 'freezer',
                'wash dishes': 'dishwasher', 'dry dishes': 'dish towel',
                'clean dishes by hand': 'sponge', 'scrub dishes': 'sponge',
                'flip pancakes': 'spatula', 'serve soup': 'ladle',
                'strain pasta': 'colander', 'drain water from food': 'colander',
                'roll dough': 'rolling pin', 'whisk eggs': 'whisk',
                # Cleaning / household
                'clean the floor': 'mop', 'sweep the floor': 'broom',
                'vacuum carpet': 'vacuum cleaner', 'clean carpets': 'vacuum cleaner',
                'dust furniture': 'duster', 'wipe surfaces': 'cloth',
                'clean windows': 'squeegee', 'wash windows': 'squeegee',
                'scrub the toilet': 'toilet brush', 'clean the bathroom': 'sponge',
                'wash clothes': 'washing machine', 'dry clothes': 'dryer',
                'iron clothes': 'iron', 'remove wrinkles from clothes': 'iron',
                'hang clothes': 'hanger', 'fold clothes': 'hands',
                'sew clothes': 'needle', 'mend clothes': 'needle',
                'cut fabric': 'scissors', 'measure fabric': 'measuring tape',
                'take out the trash': 'trash bag', 'collect garbage': 'trash can',
                # Bathroom / personal care
                'brush teeth': 'toothbrush', 'clean teeth': 'toothbrush',
                'wash hair': 'shampoo', 'condition hair': 'conditioner',
                'dry hair': 'hair dryer', 'style hair': 'comb',
                'brush hair': 'hairbrush', 'detangle hair': 'comb',
                'shave': 'razor', 'remove facial hair': 'razor',
                'cut nails': 'nail clippers', 'trim nails': 'nail clippers',
                'wash hands': 'soap', 'cleanse skin': 'soap',
                'dry hands': 'towel', 'dry body after shower': 'towel',
                'see yourself': 'mirror', 'look at your reflection': 'mirror',
                'put on makeup': 'makeup brush', 'apply lipstick': 'lipstick',
                'smell good': 'perfume', 'prevent body odor': 'deodorant',
                # Medical / health
                'measure temperature': 'thermometer', 'check for fever': 'thermometer',
                'listen to heartbeat': 'stethoscope', 'check blood pressure': 'blood pressure cuff',
                'bandage a wound': 'bandage', 'cover a cut': 'bandaid',
                'give an injection': 'syringe', 'take medicine': 'spoon',
                'see inside the body': 'x-ray', 'look at bones': 'x-ray',
                'protect hands': 'gloves', 'protect eyes': 'goggles',
                'walk with an injury': 'crutches', 'get around with broken leg': 'wheelchair',
                # Gardening / outdoor
                'dig a hole': 'shovel', 'dig in the garden': 'trowel',
                'rake leaves': 'rake', 'gather leaves': 'rake',
                'cut grass': 'lawn mower', 'mow the lawn': 'lawn mower',
                'water plants': 'watering can', 'spray plants with water': 'hose',
                'trim bushes': 'hedge trimmer', 'prune trees': 'pruning shears',
                'plant seeds': 'trowel', 'weed the garden': 'hoe',
                'chop wood': 'axe', 'split logs': 'axe',
                'cut down a tree': 'chainsaw', 'saw wood': 'saw',
                # Workshop / tools
                'drive a nail': 'hammer', 'pound nails': 'hammer',
                'turn a screw': 'screwdriver', 'tighten a bolt': 'wrench',
                'cut metal': 'hacksaw', 'drill a hole': 'drill',
                'sand wood': 'sandpaper', 'smooth surfaces': 'sandpaper',
                'measure for construction': 'tape measure', 'level a surface': 'level',
                'hold things together': 'clamp', 'grip something tightly': 'pliers',
                'cut wire': 'wire cutters', 'strip wire': 'wire stripper',
                'weld metal': 'welder', 'solder electronics': 'soldering iron',
                # Art / craft
                'paint a picture': 'paintbrush', 'color a drawing': 'crayon',
                'draw': 'pencil', 'sketch': 'pencil',
                'make pottery': 'pottery wheel', 'mold clay': 'hands',
                'take a photograph': 'camera', 'take pictures': 'camera',
                'record a video': 'camera', 'film something': 'video camera',
                'play recorded music': 'speaker', 'listen to music privately': 'headphones',
                'amplify sound': 'microphone', 'record audio': 'microphone',
                'watch movies': 'television', 'display video': 'screen',
                'read books': 'book', 'look up words': 'dictionary',
                # Navigation / travel
                'find your way': 'map', 'navigate': 'compass',
                'tell direction': 'compass', 'find north': 'compass',
                'travel on water': 'boat', 'travel underwater': 'submarine',
                'fly in the sky': 'airplane', 'travel long distances quickly': 'airplane',
                'travel on land': 'car', 'commute to work': 'car',
                'ride to school': 'bus', 'travel by train': 'train',
                'lock a door': 'key', 'unlock something': 'key',
                'open a locked door': 'key', 'secure your home': 'lock',
                # Sports / exercise
                'play tennis': 'racket', 'hit a tennis ball': 'racket',
                'play baseball': 'bat', 'hit a baseball': 'bat',
                'play golf': 'golf club', 'hit a golf ball': 'golf club',
                'play hockey': 'hockey stick', 'hit a puck': 'hockey stick',
                'catch a baseball': 'glove', 'protect your head': 'helmet',
                'swim': 'swimsuit', 'float in water': 'life jacket',
                'exercise': 'dumbbell', 'lift weights': 'barbell',
                'do yoga': 'yoga mat', 'stretch': 'yoga mat',
                # Sleep / rest
                'sleep': 'bed', 'rest your head': 'pillow',
                'keep warm at night': 'blanket', 'cover yourself': 'blanket',
                'sit down': 'chair', 'work at a desk': 'desk',
                'see in the dark': 'flashlight', 'light a room': 'lamp',
                'start a fire': 'matches', 'light a candle': 'lighter',
                # Communication
                'call someone': 'phone', 'talk to someone far away': 'phone',
                'send a letter': 'mail', 'send a message online': 'email',
                'write a letter': 'pen', 'address an envelope': 'pen',
                'mail a package': 'box', 'seal an envelope': 'glue',
                'make a phone call': 'phone', 'text someone': 'phone',
            }
            # Try exact match first, then partial
            if func in obj_table:
                ans = obj_table[func]
                log(f"[Accessibility] Use-to solver: '{func}' -> {ans}")
                return ans
            # Try partial match
            for key, ans in obj_table.items():
                if key in func or func in key:
                    log(f"[Accessibility] Use-to partial: '{func}' -> '{key}' -> {ans}")
                    return ans
            # Generic pattern: "use to X with" or "use for X"
            generic_use = re.search(
                r'(?:use|used)\s+(?:to|for)\s+(\w+(?:ing)?)\s+.*',
                t, re.IGNORECASE
            )

        # ── YES / NO QUESTIONS: "Do cans have labels?", "Is the sky blue?" ──
        yes_no = re.search(
            r'^(?:do|does|is|are|can|will|has|have|was|were)\s+(.+?)\s*[?.!]?$',
            t, re.IGNORECASE
        )
        if yes_no:
            rest = yes_no.group(1).lower().strip()
            yes_no_answers = {
                # Obvious yes
                'cans have labels': 'yes',
                'labels on cans': 'yes',
                'birds fly': 'yes',
                'fish swim': 'yes',
                'the sky blue': 'yes',
                'the sun hot': 'yes',
                'water wet': 'yes',
                'fire hot': 'yes',
                'ice cold': 'yes',
                'humans breathe air': 'yes',
                'humans need water': 'yes',
                'humans need oxygen': 'yes',
                'plants need water': 'yes',
                'plants need sunlight': 'yes',
                'dogs bark': 'yes',
                'cats meow': 'yes',
                'cows moo': 'yes',
                'snakes have no legs': 'yes',
                'spiders have eight legs': 'yes',
                'the earth round': 'yes',
                'the sun a star': 'yes',
                'a week have seven days': 'yes',
                'a year have twelve months': 'yes',
                'a day have twenty four hours': 'yes',
                'an hour have sixty minutes': 'yes',
                'a triangle have three sides': 'yes',
                'a square have four sides': 'yes',
                'humans have two eyes': 'yes',
                'humans have two ears': 'yes',
                'humans have ten fingers': 'yes',
                'cars have wheels': 'yes',
                'bicycles have two wheels': 'yes',
                'planes fly': 'yes',
                'boats float': 'yes',
                'snow cold': 'yes',
                'the moon orbit the earth': 'yes',
                'the earth orbit the sun': 'yes',
                'penguins birds': 'yes',
                'whales mammals': 'yes',
                'dolphins mammals': 'yes',
                'bats mammals': 'yes',
                'owls nocturnal': 'yes',
                'the sun rise in the east': 'yes',
                'the sun set in the west': 'yes',
                'there seven continents': 'yes',
                'there five oceans': 'yes',
                'a rainbow have seven colors': 'yes',
                'tigers have stripes': 'yes',
                'zebras have stripes': 'yes',
                'giraffes have long necks': 'yes',
                'elephants have trunks': 'yes',
                'kangaroos live in australia': 'yes',
                'polar bears live in the arctic': 'yes',
                'penguins live in antarctica': 'yes',
                # Obvious no
                'the sun cold': 'no',
                'ice hot': 'no',
                'fire cold': 'no',
                'water dry': 'no',
                'the sky green': 'no',
                'grass blue': 'no',
                'humans have tails': 'no',
                'humans can fly': 'no',
                'fish have legs': 'no',
                'birds have four legs': 'no',
                'snakes have legs': 'no',
                'dogs have wings': 'no',
                'cats bark': 'no',
                'cows fly': 'no',
                'elephants can jump': 'no',
                'pigs can fly': 'no',
                'spiders insects': 'no',
                'the earth flat': 'no',
                'the moon a planet': 'no',
                'pluto a planet': 'no',
                'a week have eight days': 'no',
                'a year have thirteen months': 'no',
                'a triangle have four sides': 'no',
                'a square have three sides': 'no',
                'humans have four eyes': 'no',
                'bicycles have four wheels': 'no',
                'cars can fly': 'no',
                'boats can fly': 'no',
                'the sun orbit the earth': 'no',
                'whales fish': 'no',
                'penguins can fly': 'no',
                'bats blind': 'no',
                'the sun rise in the west': 'no',
                'there ten continents': 'no',
                'there seven oceans': 'no',
                'lemons sweet': 'no',
                'sugar sour': 'no',
                'salt sweet': 'no',
                # -- Food & preservation --
                'canned foods preserved': 'yes',
                'foods canned preserved': 'yes',
                'canned goods preserved': 'yes',
                'food preserved by canning': 'yes',
                'preserved foods safe': 'yes',
                'pickled foods preserved': 'yes',
                'dried foods preserved': 'yes',
                'frozen foods preserved': 'yes',
                'refrigerated foods preserved': 'yes',
                'stored frozen': 'yes',
                'keep frozen': 'yes',
                'be frozen': 'yes',
                'bread stored frozen': 'yes',
                'frozen bread': 'yes',
                'food stored frozen': 'yes',
                'bread in the freezer': 'yes',
                'food in the freezer': 'yes',
                'store in the freezer': 'yes',
                'salted foods preserved': 'yes',
                'smoked foods preserved': 'yes',
                'cooked foods safe': 'yes',
                'cooked meat safe': 'yes',
                'raw chicken safe': 'no',
                'raw meat need cooking': 'yes',
                'moldy food safe': 'no',
                'expired food safe': 'no',
                'spoiled food safe': 'no',
                'food cooking kill bacteria': 'yes',
                'washing hands prevent illness': 'yes',
                # -- Everyday objects & properties --
                'glass breakable': 'yes',
                'glass transparent': 'yes',
                'metal conduct electricity': 'yes',
                'metal conductive': 'yes',
                'wood conductive': 'no',
                'rubber conduct electricity': 'no',
                'plastic biodegradable': 'no',
                'paper burn': 'yes',
                'water boil': 'yes',
                'water freezable': 'yes',
                'oil mix with water': 'no',
                'oil float on water': 'yes',
                'ice float on water': 'yes',
                'stones sink in water': 'yes',
                'rocks float': 'no',
                'magnets attract metal': 'yes',
                'magnets attract iron': 'yes',
                'magnets attract plastic': 'no',
                'magnets attract wood': 'no',
                # -- Animals & nature --
                'lions carnivores': 'yes',
                'lions eat meat': 'yes',
                'cows herbivores': 'yes',
                'cows eat grass': 'yes',
                'wolves carnivores': 'yes',
                'bears omnivores': 'yes',
                'rabbits herbivores': 'yes',
                'sheep herbivores': 'yes',
                'sharks carnivores': 'yes',
                'sharks dangerous': 'yes',
                'all sharks dangerous': 'no',
                'dolphins friendly': 'yes',
                'dolphins intelligent': 'yes',
                'dogs loyal': 'yes',
                'cats independent': 'yes',
                'mice small': 'yes',
                'whales the largest animals': 'yes',
                'blue whales the largest animals': 'yes',
                'ants live in colonies': 'yes',
                'bees make honey': 'yes',
                'bees important for pollination': 'yes',
                'butterflies come from caterpillars': 'yes',
                'moths attracted to light': 'yes',
                'spiders make webs': 'yes',
                'all spiders venomous': 'yes',
                'all spiders dangerous to humans': 'no',
                'snakes reptiles': 'yes',
                'lizards reptiles': 'yes',
                'turtles reptiles': 'yes',
                'frogs amphibians': 'yes',
                'toads amphibians': 'yes',
                'alligators dangerous': 'yes',
                'crocodiles dangerous': 'yes',
                'eagles birds of prey': 'yes',
                'owls birds of prey': 'yes',
                'parrots can talk': 'yes',
                'penguins live in cold climates': 'yes',
                'camels live in deserts': 'yes',
                'polar bears white': 'yes',
                'grizzly bears brown': 'yes',
                'giraffes the tallest animals': 'yes',
                'cheetahs the fastest land animals': 'yes',
                'sloths slow': 'yes',
                'snails slow': 'yes',
                'turtles slow': 'yes',
                'rabbits fast': 'yes',
                'horses fast': 'yes',
                # -- Human body & health --
                'humans need sleep': 'yes',
                'humans need food': 'yes',
                'humans need exercise': 'yes',
                'exercise good for health': 'yes',
                'smoking harmful': 'yes',
                'smoking bad for health': 'yes',
                'smoking cause cancer': 'yes',
                'alcohol harmful in excess': 'yes',
                'sugar bad in excess': 'yes',
                'vitamins necessary': 'yes',
                'vitamin c prevent scurvy': 'yes',
                'vitamin d from sunlight': 'yes',
                'the heart pump blood': 'yes',
                'the lungs exchange oxygen': 'yes',
                'the brain control the body': 'yes',
                'the liver filter blood': 'yes',
                'the kidneys filter waste': 'yes',
                'the skin the largest organ': 'yes',
                'the tongue detect taste': 'yes',
                'the nose detect smell': 'yes',
                'the ears detect sound': 'yes',
                'the eyes detect light': 'yes',
                'humans have five senses': 'yes',
                'bones provide structure': 'yes',
                'muscles enable movement': 'yes',
                'blood red': 'yes',
                'oxygen necessary for life': 'yes',
                'photosynthesis produce oxygen': 'yes',
                'plants produce oxygen': 'yes',
                'plants absorb carbon dioxide': 'yes',
                # -- Science & physics --
                'gravity exist': 'yes',
                'gravity pull things down': 'yes',
                'the earth have gravity': 'yes',
                'the moon have gravity': 'yes',
                'light travel faster than sound': 'yes',
                'sound travel slower than light': 'yes',
                'sound travel through air': 'yes',
                'sound travel through water': 'yes',
                'sound travel through vacuum': 'no',
                'light travel through vacuum': 'yes',
                'water expand when frozen': 'yes',
                'ice less dense than water': 'yes',
                'hot air rise': 'yes',
                'cold air sink': 'yes',
                'salt dissolve in water': 'yes',
                'sugar dissolve in water': 'yes',
                'sand dissolve in water': 'no',
                'iron rust': 'yes',
                'rust a form of corrosion': 'yes',
                'gold rust': 'no',
                'diamonds hard': 'yes',
                'diamonds the hardest natural substance': 'yes',
                'helium lighter than air': 'yes',
                'hydrogen the lightest element': 'yes',
                'uranium radioactive': 'yes',
                'the sun hot': 'yes',
                'the sun very large': 'yes',
                'the sun bigger than earth': 'yes',
                'the moon smaller than earth': 'yes',
                'the earth have one moon': 'yes',
                'jupiter the largest planet': 'yes',
                'saturn have rings': 'yes',
                'mars called the red planet': 'yes',
                'venus the hottest planet': 'yes',
                'mercury the closest planet to the sun': 'yes',
                'neptune the farthest planet': 'yes',
                'the universe expanding': 'yes',
                'black holes exist': 'yes',
                'black holes have strong gravity': 'yes',
                # -- Geography & places --
                'the sahara a desert': 'yes',
                'the sahara hot': 'yes',
                'antarctica cold': 'yes',
                'antarctica a continent': 'yes',
                'the amazon a rainforest': 'yes',
                'the amazon a river': 'yes',
                'everest the highest mountain': 'yes',
                'the nile a river': 'yes',
                'the pacific the largest ocean': 'yes',
                'australia a continent': 'yes',
                'australia a country': 'yes',
                'canada a country': 'yes',
                'canada cold': 'yes',
                'greenland an island': 'yes',
                'iceland green': 'no',
                'russia the largest country': 'yes',
                'china have the most people': 'yes',
                'india have many people': 'yes',
                'tokyo a city': 'yes',
                'paris in france': 'yes',
                'london in england': 'yes',
                'new york a city': 'yes',
                'the equator hot': 'yes',
                'the poles cold': 'yes',
                # -- Technology & computers --
                'computers use electricity': 'yes',
                'computers process data': 'yes',
                'the internet connect people': 'yes',
                'wifi wireless': 'yes',
                'bluetooth wireless': 'yes',
                'smartphones have touch screens': 'yes',
                'smartphones can make calls': 'yes',
                'smartphones can access the internet': 'yes',
                'gps use satellites': 'yes',
                'satellites orbit the earth': 'yes',
                'robots automate tasks': 'yes',
                'passwords protect accounts': 'yes',
                'encryption protect data': 'yes',
                'firewalls protect networks': 'yes',
                'viruses can infect computers': 'yes',
                'antivirus software protect computers': 'yes',
                # -- Everyday facts --
                'schools educate children': 'yes',
                'libraries have books': 'yes',
                'libraries lend books': 'yes',
                'hospitals treat patients': 'yes',
                'doctors treat illness': 'yes',
                'nurses care for patients': 'yes',
                'firefighters put out fires': 'yes',
                'police enforce laws': 'yes',
                'teachers educate students': 'yes',
                'farmers grow food': 'yes',
                'chefs cook food': 'yes',
                'pilots fly planes': 'yes',
                'drivers operate vehicles': 'yes',
                'bread made from flour': 'yes',
                'cheese made from milk': 'yes',
                'wine made from grapes': 'yes',
                'chocolate made from cocoa': 'yes',
                'butter made from cream': 'yes',
                'pasta made from flour': 'yes',
                'rice a grain': 'yes',
                'wheat a grain': 'yes',
                'corn a grain': 'yes',
                'potatoes vegetables': 'yes',
                'tomatoes fruits': 'yes',
                'strawberries fruits': 'yes',
                'bananas fruits': 'yes',
                'apples fruits': 'yes',
                'oranges citrus fruits': 'yes',
                'lemons citrus fruits': 'yes',
                'broccoli a vegetable': 'yes',
                'spinach a vegetable': 'yes',
                'carrots vegetables': 'yes',
                'onions vegetables': 'yes',
                'garlic a vegetable': 'yes',
                'mushrooms fungi': 'yes',
                'yeast a fungus': 'yes',
                'bacteria microscopic': 'yes',
                'bacteria can cause disease': 'yes',
                'viruses microscopic': 'yes',
                'viruses cause illness': 'yes',
                'vaccines prevent disease': 'yes',
                'antibiotics kill bacteria': 'yes',
                'antibiotics kill viruses': 'no',
                # -- More negations --
                'glass unbreakable': 'no',
                'water flammable': 'no',
                'rocks edible': 'no',
                'plastic edible': 'no',
                'metal edible': 'no',
                'sand edible': 'no',
                'humans can breathe underwater': 'no',
                'humans can breathe in space': 'no',
                'oxygen flammable': 'no',
                'hydrogen not flammable': 'no',
                'the sun cold': 'no',
                'the moon hot': 'no',
                'steel lighter than aluminum': 'no',
                'feather heavier than lead': 'no',
                'birds mammals': 'no',
                'whales fish': 'no',
                'dolphins fish': 'no',
                'bats birds': 'no',
                'penguins can fly': 'no',
                'ostriches can fly': 'no',
                'chickens can fly long distances': 'no',
                'spiders insects': 'no',
                'snakes have legs': 'no',
                'snakes have eyelids': 'no',
                'worms have legs': 'no',
                'frogs reptiles': 'no',
                'lizards amphibians': 'no',
                'mammals lay eggs': 'no',
                'all mammals lay eggs': 'no',
                'platypus a reptile': 'no',
                'sharks mammals': 'no',
                'the earth the center of the universe': 'no',
                'the sun orbit the earth': 'no',
                'the moon emit light': 'no',
                'the moon a planet': 'no',
                'pluto a planet in the current classification': 'no',
                'stars small': 'no',
                'the ocean freshwater': 'no',
                'rivers saltwater': 'no',
                'lakes saltwater': 'no',
                'mountains flat': 'no',
                'deserts wet': 'no',
                'rainforests dry': 'no',
                'computers work without electricity': 'no',
                'diamonds cheap': 'no',
                'gold cheap': 'no',
                'gravity does not exist': 'no',
                'light slower than sound': 'no',
                'sound faster than light': 'no',
                'the earth flat': 'no',
                'humans can survive without water': 'no',
                'humans can survive without air': 'no',
                'humans can survive without food indefinitely': 'no',
                'plants do not need sunlight': 'no',
                'plants do not need water': 'no',
                'animals do not need water': 'no',
                'animals do not need food': 'no',
            }
            for key, ans in yes_no_answers.items():
                if key in rest:
                    log(f"[Accessibility] Yes/No: '{rest[:60]}' -> {ans}")
                    return ans
            # Generic pattern match
            obvious_yes = re.search(
                r'\b(?:have|has|can|do|does|is|are|be|keep|always|definitely|obviously)\b',
                rest
            )
            if obvious_yes and not re.search(r'\b(?:no|not|never|none|can["\']?t|cannot|don["\']?t|doesn["\']?t|isn["\']?t|aren["\']?t|won["\']?t)\b', rest):
                # Common sense heuristic: preservation/cooking words → yes
                if re.search(r'\b(?:preserv|refrigerat|freez|froz\w*|freezer|canning|cook|bak|dry|salt|cure|pickl|smok|seal|packag|stor(?:e|ed|age|es|ing))\b', rest):
                    log(f"[Accessibility] Yes/No common-sense: '{rest[:60]}' -> yes")
                    return 'yes'
                if re.search(r'\b(?:die|dead|death|broken|impossible|fake|nonexist)\b', rest):
                    log(f"[Accessibility] Yes/No common-sense: '{rest[:60]}' -> no")
                    return 'no'
                # Don't guess - let it fall through to KB
        # ── TRUE / FALSE questions ──
        true_false = re.search(
            r'(?:is it true|true or false|is the following (?:statement )?true|is this true)[:\s]*(.+)',
            t, re.IGNORECASE
        )
        if true_false:
            statement = true_false.group(1).lower().strip()
            # Same logic as yes/no
            for key, ans in yes_no_answers.items():
                if key in statement:
                    tf = 'true' if ans == 'yes' else 'false'
                    log(f"[Accessibility] True/False: '{statement[:60]}' -> {tf}")
                    return tf

        # ── WORD COUNT: "How many words are in this sentence?" ──
        wc_match = re.search(
            r'how many words\s+(?:are|in)\s+(?:in\s+)?(?:this|the|the following)\s+(?:sentence|phrase|question)["\']?(.{3,})',
            t, re.IGNORECASE
        )
        if wc_match:
            sentence = wc_match.group(1).strip().rstrip('?.').strip()
            word_count = len(sentence.split())
            log(f"[Accessibility] Word count: '{sentence[:60]}' = {word_count} words")
            return str(word_count)

        # ── PLURAL: "What is the plural of X?" ──
        plural = re.search(
            r'(?:what|which)\s+(?:is|are)\s+(?:the\s+)?plural\s+(?:of|form of|for)\s+(?:the\s+word\s+)?["\']?(\w{2,})',
            t, re.IGNORECASE
        )
        if plural:
            word = plural.group(1).lower()
            plurals = {
                'child': 'children', 'man': 'men', 'woman': 'women',
                'mouse': 'mice', 'goose': 'geese', 'tooth': 'teeth',
                'foot': 'feet', 'person': 'people', 'ox': 'oxen',
                'leaf': 'leaves', 'wolf': 'wolves', 'knife': 'knives',
                'wife': 'wives', 'life': 'lives', 'calf': 'calves',
                'half': 'halves', 'loaf': 'loaves', 'shelf': 'shelves',
                'thief': 'thieves', 'self': 'selves', 'elf': 'elves',
                'sheep': 'sheep', 'deer': 'deer', 'fish': 'fish',
                'moose': 'moose', 'species': 'species', 'series': 'series',
                'cactus': 'cacti', 'fungus': 'fungi', 'nucleus': 'nuclei',
                'radius': 'radii', 'stimulus': 'stimuli', 'syllabus': 'syllabi',
                'alumnus': 'alumni', 'focus': 'foci', 'datum': 'data',
                'criterion': 'criteria', 'phenomenon': 'phenomena',
                'analysis': 'analyses', 'thesis': 'theses', 'hypothesis': 'hypotheses',
                'bacterium': 'bacteria', 'medium': 'media', 'memorandum': 'memoranda',
                'appendix': 'appendices', 'index': 'indices', 'matrix': 'matrices',
                'vertex': 'vertices', 'vortex': 'vortices',
                'bus': 'buses', 'box': 'boxes', 'watch': 'watches',
                'bus': 'buses', 'dish': 'dishes', 'church': 'churches',
                'tomato': 'tomatoes', 'potato': 'potatoes', 'hero': 'heroes',
                'echo': 'echoes', 'veto': 'vetoes', 'mango': 'mangoes',
            }
            if word in plurals:
                ans = plurals[word]
                log(f"[Accessibility] Plural: '{word}' -> '{ans}'")
                return ans
            # Default: add -s or -es
            if word.endswith(('s', 'x', 'z', 'ch', 'sh')):
                log(f"[Accessibility] Plural default: '{word}' -> '{word}es'")
                return word + 'es'
            elif word.endswith('y') and len(word) > 2 and word[-2] not in 'aeiou':
                log(f"[Accessibility] Plural default: '{word}' -> '{word[:-1]}ies'")
                return word[:-1] + 'ies'
            else:
                log(f"[Accessibility] Plural default: '{word}' -> '{word}s'")
                return word + 's'

        # ── PAST TENSE: "What is the past tense of X?" ──
        past_tense = re.search(
            r'(?:what|which)\s+(?:is|are)\s+(?:the\s+)?past\s+(?:tense|form)\s+(?:of|for)\s+(?:the\s+word\s+)?["\']?(\w{2,})',
            t, re.IGNORECASE
        )
        if past_tense:
            word = past_tense.group(1).lower()
            past = {
                'go': 'went', 'be': 'was', 'have': 'had',
                'do': 'did', 'say': 'said', 'get': 'got',
                'make': 'made', 'know': 'knew', 'think': 'thought',
                'take': 'took', 'see': 'saw', 'come': 'came',
                'find': 'found', 'give': 'gave', 'tell': 'told',
                'feel': 'felt', 'become': 'became', 'leave': 'left',
                'put': 'put', 'mean': 'meant', 'keep': 'kept',
                'let': 'let', 'begin': 'began', 'show': 'showed',
                'hear': 'heard', 'play': 'played', 'run': 'ran',
                'move': 'moved', 'live': 'lived', 'believe': 'believed',
                'hold': 'held', 'bring': 'brought', 'happen': 'happened',
                'write': 'wrote', 'provide': 'provided', 'sit': 'sat',
                'stand': 'stood', 'lose': 'lost', 'pay': 'paid',
                'meet': 'met', 'include': 'included', 'continue': 'continued',
                'set': 'set', 'learn': 'learned', 'change': 'changed',
                'lead': 'led', 'understand': 'understood', 'watch': 'watched',
                'follow': 'followed', 'stop': 'stopped', 'create': 'created',
                'speak': 'spoke', 'read': 'read', 'allow': 'allowed',
                'add': 'added', 'spend': 'spent', 'grow': 'grew',
                'open': 'opened', 'walk': 'walked', 'win': 'won',
                'offer': 'offered', 'remember': 'remembered', 'love': 'loved',
                'consider': 'considered', 'appear': 'appeared', 'buy': 'bought',
                'wait': 'waited', 'serve': 'served', 'die': 'died',
                'send': 'sent', 'expect': 'expected', 'build': 'built',
                'stay': 'stayed', 'fall': 'fell', 'cut': 'cut',
                'reach': 'reached', 'kill': 'killed', 'remain': 'remained',
                'suggest': 'suggested', 'raise': 'raised', 'pass': 'passed',
                'sell': 'sold', 'require': 'required', 'report': 'reported',
                'decide': 'decided', 'pull': 'pulled', 'break': 'broke',
                'receive': 'received', 'agree': 'agreed', 'hit': 'hit',
                'force': 'forced', 'refuse': 'refused', 'thank': 'thanked',
                'choose': 'chose', 'fly': 'flew', 'drink': 'drank',
                'eat': 'ate', 'sleep': 'slept', 'swim': 'swam',
                'drive': 'drove', 'draw': 'drew', 'sing': 'sang',
                'ride': 'rode', 'ring': 'rang', 'shake': 'shook',
                'steal': 'stole', 'wear': 'wore', 'tear': 'tore',
                'throw': 'threw', 'blow': 'blew', 'freeze': 'froze',
            }
            if word in past:
                ans = past[word]
                log(f"[Accessibility] Past tense: '{word}' -> '{ans}'")
                return ans
            # Regular: add -ed
            if word.endswith('e'):
                ans = word + 'd'
            elif word.endswith('y') and len(word) > 2 and word[-2] not in 'aeiou':
                ans = word[:-1] + 'ied'
            elif len(word) >= 3 and word[-1] not in 'aeiouwy' and word[-2] in 'aeiou' and word[-3] not in 'aeiou':
                ans = word + word[-1] + 'ed'
            else:
                ans = word + 'ed'
            log(f"[Accessibility] Past tense default: '{word}' -> '{ans}'")
            return ans

        # ── SYLLABLE COUNT: "How many syllables in X?" ──
        syllable = re.search(
            r'how many syllables\s+(?:are\s+)?(?:in\s+)?(?:the\s+word\s+)?["\']?(\w{2,})',
            t, re.IGNORECASE
        )
        if syllable:
            word = syllable.group(1).lower()
            # Simple vowel-group counting
            vowels = re.findall(r'[aeiouy]+', word)
            count = len(vowels)
            if word.endswith('e') and count > 1:
                count -= 1  # silent e
            if word.endswith('le') and len(word) > 2 and word[-3] not in 'aeiou':
                count += 1
            count = max(1, count)
            log(f"[Accessibility] Syllable count: '{word}' = {count}")
            return str(count)

        # ── STRUCTURE / PLACE SOLVER: "What structure crosses over water?" ──
        m = re.search(
            r'(?:what|which)\s+(?:structure|building|construction|thing|place)'
            r'\s+(?:cross(?:es)?|goes?\s+(?:over|across|under)|spans?|connects?)\s+(?:over\s+)?'
            r'(?:a\s+)?(?:river|water|road|valley|ocean|sea|lake|railway|highway)',
            t, re.IGNORECASE)
        if m:
            full = orig.strip()
            # bridge is the primary answer for "crosses over water"
            if re.search(r'water|river|lake|ocean|sea', full, re.IGNORECASE):
                log("[Accessibility] Structure solver: crosses water → bridge")
                return "bridge"
            if re.search(r'road|highway|railway|train', full, re.IGNORECASE):
                log("[Accessibility] Structure solver: crosses road → bridge")
                return "bridge"
            if re.search(r'valley|gorge|canyon', full, re.IGNORECASE):
                log("[Accessibility] Structure solver: crosses valley → bridge")
                return "bridge"
            if re.search(r'mountain|hill', full, re.IGNORECASE) and re.search(r'through|under', full, re.IGNORECASE):
                log("[Accessibility] Structure solver: goes through mountain → tunnel")
                return "tunnel"
            log("[Accessibility] Structure solver: default → bridge")
            return "bridge"

        # ── WHAT BUILDING / PLACE: "What building is used to borrow books?" ──
        m = re.search(
            r'(?:what|which)\s+(?:building|place|location|venue|structure|facility)'
            r'\s+(?:is|do|are|can|would|does)',
            t, re.IGNORECASE)
        if m:
            full = orig.strip().lower()
            # Quick keyword-based dispatch for common building/place questions
            place_map = {
                (r'borrow.*book|read.*book|library', 'library'),
                (r'buy.*grocer|shop.*food|supermarket', 'supermarket'),
                (r'buy.*(?:cloth|shoe|dress|shirt)', 'mall'),
                (r'doctor|sick|ill|hospital|nurse|patient|treated', 'hospital'),
                (r'learn|study|school|t(ea)?cher|student|class', 'school'),
                (r'pray|worship|religio|church|temple|mosque', 'church'),
                (r'swim|pool|swimming', 'pool'),
                (r'exercise|workout|gym|fitness|weight', 'gym'),
                (r'movie|film|cinema|theater', 'cinema'),
                (r'fly|plane|airplane|airport|take.*off|land', 'airport'),
                (r'train|railway|station', 'station'),
                (r'bus.*stop|bus.*station', 'bus station'),
                (r'boat|ship|dock|port|harbor', 'port'),
                (r'sleep|hotel|motel|inn|lodging', 'hotel'),
                (r'eat.*out|restaurant|cafe|diner|dining', 'restaurant'),
                (r'money|bank|deposit|withdraw|saving', 'bank'),
                (r'letter|mail|post|stamp|parcel', 'post office'),
                (r'gas|petrol|fuel|fill.*tank', 'gas station'),
                (r'park.*car|parking', 'parking lot'),
                (r'museum|art.*exhibit|dinosaur', 'museum'),
                (r'zoo|animal.*see|animal.*visit', 'zoo'),
                (r'garden|flower|plant.*grow.*park', 'park'),
                (r'court|judge|trial|lawyer|legal', 'courthouse'),
                (r'jail|prison|inmate|cell', 'prison'),
                (r'fire.*station|firefighter', 'fire station'),
                (r'police.*station|police.*officer', 'police station'),
                (r'stadium|sport.*play|football.*field|baseball.*field', 'stadium'),
                (r'farm|crop|harvest|animal.*raise', 'farm'),
                (r'factory|manufactur|assemble|product.*made', 'factory'),
                (r'store|shop|buy|purchase|sell', 'store'),
            }
            for (pattern, answer) in place_map:
                if re.search(pattern, full, re.IGNORECASE):
                    log(f"[Accessibility] Building/place solver: {answer}")
                    return answer

        # ── WHAT DO YOU DO: "What do you use to..." / "What do you do with..." ──
        m = re.search(
            r'(?:what|which)\s+(?:do\s+you|can\s+you|would\s+you|does\s+one)'
            r'\s+(?:do|use)\s+(?:with|to|for|when)',
            t, re.IGNORECASE)
        if m:
            full = orig.strip().lower()
            action_map = {
                (r'key.*door|door.*key|lock.*door|door.*lock', 'unlock'),
                (r'pen.*(?:write|paper)|pencil.*(?:write|paper)', 'write'),
                (r'knife.*cut.*(?:food|vegetable|fruit|bread|meat)', 'cut'),
                (r'scissors.*cut.*(?:paper|hair|cloth|fabric)', 'cut'),
                (r'broom.*(?:floor|clean|sweep)', 'sweep'),
                (r'mop.*(?:floor|clean)', 'mop'),
                (r'phone.*(?:call|talk|text|message)', 'call'),
                (r'oven.*(?:bake|cook|heat)', 'bake'),
                (r'stove.*(?:cook|boil|fry|heat)', 'cook'),
                (r'fridge.*(?:food|cool|store|keep)', 'store'),
                (r'camera.*(?:picture|photo|image|film)', 'photograph'),
                (r'paint.*(?:wall|picture|art|canvas)', 'paint'),
                (r'hammer.*(?:nail|wood|hit|hit)', 'hammer'),
                (r'umbrella.*(?:rain|wet|dry)', 'protect'),
                (r'lamp.*(?:light|dark|see|bright)', 'light'),
                (r'bed.*(?:sleep|rest|lie)', 'sleep'),
                (r'chair.*(?:sit|seat)', 'sit'),
                (r'spoon.*(?:eat|stir|soup)', 'eat'),
                (r'fork.*(?:eat|food)', 'eat'),
                (r'glass.*(?:drink|water)', 'drink'),
                (r'cup.*(?:drink|coffee|tea)', 'drink'),
                (r'book.*(?:read|learn)', 'read'),
                (r'tv.*(?:watch|show|program)', 'watch'),
                (r'radio.*(?:listen|music|news)', 'listen'),
                (r'computer.*(?:type|work|internet)', 'work'),
                (r'microwave.*(?:heat|food|cook)', 'heat'),
                (r'toaster.*(?:bread|toast)', 'toast'),
                (r'kettle.*(?:water|boil|tea)', 'boil'),
                (r'iron.*(?:cloth|shirt|wrinkl)', 'iron'),
                (r'shower.*(?:wash|clean|bath)', 'wash'),
                (r'soap.*(?:wash|clean|hand)', 'wash'),
                (r'toothbrush.*(?:teeth|tooth|brush)', 'brush'),
            }
            for (pattern, answer) in action_map:
                if re.search(pattern, full, re.IGNORECASE):
                    log(f"[Accessibility] Action solver: {answer}")
                    return answer

        # ── WHAT IS X MADE OF: material/composition solver ──
        m = re.search(
            r'what\s+(?:is|are)\s+(\w+(?:\s+\w+){0,3})\s+(?:made\s+(?:of|from)|composed\s+of)',
            t, re.IGNORECASE)
        if m:
            item = m.group(1).strip().lower()
            material_map = {
                'paper': 'wood',
                'glass': 'sand',
                'plastic': 'oil',
                'steel': 'iron',
                'wine': 'grapes',
                'bread': 'flour',
                'cheese': 'milk',
                'butter': 'cream',
                'yogurt': 'milk',
                'chocolate': 'cocoa',
                'silk': 'silkworms',
                'wool': 'sheep',
                'leather': 'cowhide',
                'cotton': 'cotton plant',
                'rubber': 'rubber tree',
                'concrete': 'cement',
                'brick': 'clay',
                'porcelain': 'clay',
                'ceramic': 'clay',
                'jelly': 'fruit',
                'jam': 'fruit',
                'tofu': 'soybeans',
                'soy sauce': 'soybeans',
                'sushi': 'rice',
                'pasta': 'flour',
                'noodles': 'flour',
                'beer': 'barley',
                'whiskey': 'grain',
                'vodka': 'potatoes',
                'rum': 'sugarcane',
                'tequila': 'agave',
                'candle': 'wax',
                'soap': 'fat',
                'pencil': 'graphite',
                'diamond': 'carbon',
                'pearl': 'oyster',
                'amber': 'tree resin',
                'honey': 'nectar',
                'maple syrup': 'maple sap',
                'flour': 'wheat',
                'oil': 'olives',
                'vinegar': 'grapes',
                'gold': 'ore',
                'silver': 'ore',
                'aluminum': 'bauxite',
                'ink': 'dye',
                'paint': 'pigment',
                'rope': 'fibers',
                'linen': 'flax',
                'denim': 'cotton',
                'cork': 'cork tree',
                'sugar': 'sugarcane',
                'salt': 'sea',
                'oxygen': 'air',
            }
            for obj, mat in material_map.items():
                if obj in item:
                    log(f"[Accessibility] Material solver: {item} → {mat}")
                    return mat

        # ── HOW MANY / COUNT SOLVER: "How many X does Y have?" ──
        m = re.search(
            r'how\s+many\s+(\w+(?:\s+\w+){0,4})\s+(?:do(?:es)?|are|is|can|would|does)',
            t, re.IGNORECASE)
        if m:
            full = orig.strip().lower()
            count_map = {
                (r'wheel.*car|car.*wheel', '4'),
                (r'wheel.*bike|bicycle.*wheel', '2'),
                (r'wheel.*tricycle', '3'),
                (r'wheel.*motorcycle', '2'),
                (r'wheel.*truck', '4'),
                (r'wheel.*bus', '4'),
                (r'wheel.*unicycle', '1'),
                (r'leg.*spider|spider.*leg', '8'),
                (r'leg.*insect|insect.*leg', '6'),
                (r'leg.*ant|ant.*leg', '6'),
                (r'leg.*dog|dog.*leg', '4'),
                (r'leg.*cat|cat.*leg', '4'),
                (r'leg.*human|human.*leg|person.*leg', '2'),
                (r'leg.*bird|bird.*leg', '2'),
                (r'leg.*octopus', '8'),
                (r'leg.*centipede', '100'),
                (r'leg.*horse|horse.*leg', '4'),
                (r'leg.*cow|cow.*leg', '4'),
                (r'leg.*chair|chair.*leg', '4'),
                (r'leg.*table|table.*leg', '4'),
                (r'side.*triangle|triangle.*side', '3'),
                (r'side.*square|square.*side', '4'),
                (r'side.*pentagon', '5'),
                (r'side.*hexagon', '6'),
                (r'side.*heptagon', '7'),
                (r'side.*octagon', '8'),
                (r'side.*cube|cube.*side', '6'),
                (r'face.*cube|cube.*face', '6'),
                (r'edge.*cube|cube.*edge', '12'),
                (r'continent.*earth|earth.*continent', '7'),
                (r'ocean.*earth|earth.*ocean', '5'),
                (r'planet.*solar|solar.*planet', '8'),
                (r'color.*rainbow|rainbow.*color', '7'),
                (r'day.*week|week.*day', '7'),
                (r'month.*year|year.*month', '12'),
                (r'hour.*day|day.*hour', '24'),
                (r'minute.*hour|hour.*minute', '60'),
                (r'second.*minute|minute.*second', '60'),
                (r'day.*february|february.*day', '28'),
                (r'day.*leap.*year|leap.*year.*day', '366'),
                (r'bone.*human|human.*bone', '206'),
                (r'muscle.*human|human.*muscle', '600'),
                (r'tooth.*adult|adult.*tooth', '32'),
                (r'tooth.*child|child.*tooth|baby.*tooth', '20'),
                (r'string.*guitar|guitar.*string', '6'),
                (r'string.*violin|violin.*string', '4'),
                (r'string.*bass.*guitar|bass.*string', '4'),
                (r'string.*harp|harp.*string', '47'),
                (r'key.*piano|piano.*key', '88'),
                (r'square.*chess|chess.*square', '64'),
                (r'piece.*chess|chess.*piece', '32'),
                (r'state.*united.*state|us.*state|usa.*state', '50'),
                (r'star.*american.*flag|us.*flag.*star|usa.*flag.*star', '50'),
                (r'stripe.*american.*flag|us.*flag.*stripe|usa.*flag.*stripe', '13'),
                (r'player.*basketball.*team|basketball.*player.*court', '5'),
                (r'player.*soccer.*team|soccer.*player.*field', '11'),
                (r'player.*football.*team|football.*player.*field', '11'),
                (r'player.*baseball.*team|baseball.*player.*field', '9'),
                (r'player.*hockey.*team|hockey.*player.*ice', '6'),
                (r'player.*volleyball.*team|volleyball.*player', '6'),
                (r'ring.*olympic|olympic.*ring', '5'),
                (r'eye.*human|human.*eye', '2'),
                (r'ear.*human|human.*ear', '2'),
                (r'nose.*human|human.*nose', '1'),
                (r'mouth.*human|human.*mouth', '1'),
                (r'finger.*human.*hand|human.*hand.*finger', '5'),
                (r'toe.*human.*foot|human.*foot.*toe', '5'),
                (r'chamber.*heart|heart.*chamber', '4'),
                (r'valve.*heart|heart.*valve', '4'),
                (r'lobe.*brain|brain.*lobe', '4'),
                (r'vertebra.*human.*spine|spine.*vertebra', '33'),
                (r'rib.*human|human.*rib', '24'),
                (r'season.*year|year.*season', '4'),
                (r'quarter.*dollar|dollar.*quarter', '4'),
                (r'nickel.*dollar|dollar.*nickel', '20'),
                (r'dime.*dollar|dollar.*dime', '10'),
                (r'penny.*dollar|dollar.*penny|cent.*dollar', '100'),
                (r'semitone.*octave|octave.*semitone', '12'),
                (r'note.*octave|octave.*note', '8'),
                (r'wonder.*ancient.*world|ancient.*wonder', '7'),
                (r'book.*bible|bible.*book', '66'),
                (r'commandment.*bible|ten.*commandment', '10'),
                (r'apostle.*jesus|jesus.*apostle', '12'),
                (r'petal.*rose|rose.*petal', '5'),
                (r'leaf.*clover|clover.*leaf', '3'),
                (r'wing.*butterfly|butterfly.*wing', '4'),
                (r'wing.*bee|bee.*wing', '4'),
                (r'wing.*bird|bird.*wing', '2'),
                (r'wing.*airplane|airplane.*wing', '2'),
                (r'engine.*airplane|airplane.*engine', '2'),
                (r'engine.*boeing.*747|747.*engine', '4'),
                (r'wheel.*747|747.*wheel', '18'),
            }
            for (pattern, answer) in count_map.items():
                if re.search(pattern, full, re.IGNORECASE):
                    log(f"[Accessibility] Count solver: {answer}")
                    return answer

        # ── WHAT DOES X SOUND LIKE / SOUND SOLVER ──
        m = re.search(
            r'what\s+(?:sound|noise)\s+(?:does|do|would)\s+(\w+(?:\s+\w+){0,2})\s+make',
            t, re.IGNORECASE)
        if m:
            animal = m.group(1).strip().lower()
            sound_map = {
                'dog': 'bark', 'cat': 'meow', 'cow': 'moo', 'pig': 'oink',
                'sheep': 'baa', 'horse': 'neigh', 'duck': 'quack', 'chicken': 'cluck',
                'rooster': 'crow', 'bird': 'chirp', 'bee': 'buzz', 'snake': 'hiss',
                'lion': 'roar', 'tiger': 'roar', 'wolf': 'howl', 'owl': 'hoot',
                'frog': 'croak', 'cricket': 'chirp', 'mouse': 'squeak', 'elephant': 'trumpet',
                'monkey': 'chatter', 'donkey': 'bray', 'goat': 'bleat', 'turkey': 'gobble',
                'crow': 'caw', 'dolphin': 'click', 'whale': 'sing', 'bear': 'growl',
                'seal': 'bark', 'penguin': 'squawk',
            }
            for name, sound in sound_map.items():
                if name in animal:
                    log(f"[Accessibility] Sound solver: {animal} → {sound}")
                    return sound

        # ── COLOR SOLVER: "What color are egg whites?" ──
        m = re.search(
            r'what\s+color\s+(?:is|are|would|can)\s+(?:a\s+|an\s+|the\s+)?(\w+(?:\s+\w+){0,3})',
            t, re.IGNORECASE)
        if m:
            item = m.group(1).strip().lower()
            color_map = {
                'egg white': 'white', 'egg whites': 'white',
                'egg yolk': 'yellow', 'egg yolks': 'yellow',
                'sky': 'blue', 'sky daytime': 'blue', 'daytime sky': 'blue',
                'sky night': 'black', 'night sky': 'black',
                'sun': 'yellow', 'sunset': 'orange',
                'grass': 'green', 'grass healthy': 'green',
                'snow': 'white', 'milk': 'white', 'salt': 'white', 'sugar': 'white',
                'cloud': 'white', 'clouds': 'white', 'cotton': 'white',
                'coal': 'black', 'charcoal': 'black', 'night': 'black',
                'blood': 'red', 'rose': 'red', 'tomato': 'red', 'apple': 'red',
                'strawberry': 'red', 'cherry': 'red', 'fire truck': 'red',
                'fire hydrant': 'red', 'ruby': 'red', 'cardinal': 'red',
                'ocean': 'blue', 'sea': 'blue', 'water': 'blue',
                'blueberry': 'blue', 'blue jay': 'blue', 'sapphire': 'blue',
                'lemon': 'yellow', 'banana': 'yellow', 'sunflower': 'yellow',
                'gold': 'yellow', 'butter': 'yellow', 'corn': 'yellow',
                'orange fruit': 'orange', 'carrot': 'orange', 'pumpkin': 'orange',
                'tangerine': 'orange', 'goldfish': 'orange',
                'grass dead': 'brown', 'dead grass': 'brown', 'dirt': 'brown',
                'chocolate': 'brown', 'coffee': 'brown', 'wood': 'brown',
                'mud': 'brown', 'tree trunk': 'brown', 'bear': 'brown',
                'grape': 'purple', 'eggplant': 'purple', 'plum': 'purple',
                'amethyst': 'purple', 'lavender': 'purple', 'violet': 'purple',
                'leaf': 'green', 'leaves': 'green', 'lettuce': 'green',
                'cucumber': 'green', 'broccoli': 'green', 'emerald': 'green',
                'avocado': 'green', 'lime': 'green', 'spinach': 'green',
                'pea': 'green', 'peas': 'green', 'cactus': 'green',
                'flamingo': 'pink', 'pig': 'pink', 'bubblegum': 'pink',
                'cotton candy': 'pink', 'rose pink': 'pink',
                'zebra': 'black and white', 'panda': 'black and white',
                'penguin': 'black and white', 'newspaper': 'black and white',
                'tuxedo': 'black and white', 'dalmatian': 'black and white',
                'amber': 'orange', 'honey': 'golden', 'rust': 'brownish red',
                'bronze': 'brown', 'silver': 'gray', 'ash': 'gray',
                'elephant': 'gray', 'cement': 'gray', 'pavement': 'gray',
                'metal': 'gray', 'steel': 'gray', 'iron': 'gray',
                'lead': 'gray', 'pewter': 'gray', 'smoke': 'gray',
                'fog': 'gray', 'mist': 'gray',
                'rainbow': 'multicolor', 'prism': 'multicolor',
                'autumn leaf': 'orange', 'fall leaf': 'orange',
                'sunrise': 'orange', 'dawn': 'orange',
                'skin': 'peach', 'salmon': 'pink', 'coral': 'orange',
                'turquoise': 'blue green', 'teal': 'blue green',
                'indigo': 'dark blue', 'navy': 'dark blue', 'denim': 'blue',
                'maroon': 'dark red', 'burgundy': 'dark red', 'wine': 'dark red',
                'beige': 'tan', 'tan': 'light brown', 'cream': 'white',
                'ivory': 'white', 'vanilla': 'white', 'chalk': 'white',
                'snow white': 'white', 'albino': 'white',
                'crow': 'black', 'raven': 'black', 'panther': 'black',
                'orchid': 'purple', 'lilac': 'purple', 'mauve': 'pale purple',
                'mustard': 'yellow', 'daffodil': 'yellow', 'dandelion': 'yellow',
                'pineapple': 'yellow', 'bee': 'yellow and black',
                'ladybug': 'red and black', 'ladybird': 'red and black',
                'poppy': 'red', 'radish': 'red', 'beet': 'red',
                'raspberry': 'red', 'cranberry': 'red', 'pomegranate': 'red',
                'watermelon inside': 'red', 'watermelon outside': 'green',
                'kiwi outside': 'brown', 'kiwi inside': 'green',
                'coconut outside': 'brown', 'coconut inside': 'white',
                'mango': 'yellow', 'papaya': 'orange', 'apricot': 'orange',
                'peach': 'orange', 'nectarine': 'orange',
                'blackberry': 'black', 'olive': 'green', 'marble': 'white',
                'quartz': 'white', 'granite': 'gray', 'obsidian': 'black',
            }
            # Exact match first
            if item in color_map:
                ans = color_map[item]
                log(f"[Accessibility] Color solver exact: {item} → {ans}")
                return ans
            # Substring match
            for obj, col in color_map.items():
                if obj in item or item in obj:
                    log(f"[Accessibility] Color solver partial: '{item}' → '{col}' (matched '{obj}')")
                    return col

        # ── COMPARISON SOLVER: "Which is bigger, X or Y?" ──
        m = re.search(
            r'which\s+(?:is|are)\s+(bigger|smaller|larger|heavier|lighter|taller|shorter|'
            r'longer|faster|slower|older|younger|hotter|colder|stronger|weaker|'
            r'darker|brighter|louder|quieter|higher|lower|deeper|shallower|'
            r'wider|narrower|thicker|thinner|harder|softer|richer|poorer)',
            t, re.IGNORECASE)
        if m:
            adj = m.group(1).lower()
            # Find X or Y pattern
            items = re.findall(r'([a-zA-Z]+(?:\s+[a-zA-Z]+){0,2})\s+or\s+([a-zA-Z]+(?:\s+[a-zA-Z]+){0,2})', t, re.IGNORECASE)
            if items:
                a, b = items[0][0].strip().lower(), items[0][1].strip().lower()
                # size comparisons
                known = {
                    'bigger': {'elephant': 'mouse', 'whale': 'shark', 'sun': 'earth', 'jupiter': 'earth',
                               'earth': 'moon', 'ocean': 'lake', 'mountain': 'hill', 'lion': 'cat',
                               'bear': 'dog', 'truck': 'car', 'bus': 'car', 'train': 'bus',
                               'airplane': 'helicopter', 'blue whale': 'elephant'},
                    'smaller': {'mouse': 'elephant', 'ant': 'human', 'atom': 'cell', 'cell': 'organ',
                                'earth': 'sun', 'moon': 'earth', 'cat': 'lion'},
                    'heavier': {'elephant': 'mouse', 'whale': 'shark', 'truck': 'car', 'gold': 'feather',
                                'rock': 'feather', 'iron': 'wood', 'water': 'air', 'brick': 'feather'},
                    'lighter': {'feather': 'rock', 'air': 'water', 'helium': 'air', 'cotton': 'iron',
                                'paper': 'stone', 'mouse': 'elephant'},
                    'taller': {'giraffe': 'dog', 'skyscraper': 'house', 'tree': 'bush',
                               'mountain': 'hill', 'basketball player': 'jockey'},
                    'faster': {'cheetah': 'turtle', 'airplane': 'car', 'car': 'bicycle',
                               'light': 'sound', 'hare': 'tortoise', 'rocket': 'airplane'},
                    'slower': {'turtle': 'cheetah', 'snail': 'rabbit', 'tortoise': 'hare',
                               'walking': 'running', 'bicycle': 'car'},
                    'older': {'grandfather': 'son', 'pyramid': 'skyscraper', 'dinosaur': 'human',
                              'earth': 'human', 'stone': 'paper', 'ancient rome': 'new york'},
                    'hotter': {'sun': 'earth', 'fire': 'ice', 'lava': 'water', 'oven': 'fridge',
                               'summer': 'winter', 'desert': 'arctic', 'coffee': 'iced tea'},
                    'colder': {'ice': 'fire', 'winter': 'summer', 'arctic': 'desert', 'fridge': 'oven',
                               'antarctica': 'sahara'},
                }
                for (av, bv) in known.get(adj, {}).items():
                    if a == av and b == bv:
                        log(f"[Accessibility] Comparison: {a} {adj} than {b} → {a}")
                        return a
                    if a == bv and b == av:
                        log(f"[Accessibility] Comparison: {b} {adj} than {a} → {b}")
                        return b

        # ── TYPE / KIND / CATEGORY SOLVER: "What type of animal is a frog?" ──
        m = re.search(
            r'what\s+(?:type|kind|category|class|sort|variety|species|breed|group)'
            r'\s+(?:of|is|are)\s+(?:a\s+|an\s+|the\s+)?(\w+(?:\s+\w+){0,3})',
            t, re.IGNORECASE)
        if m:
            item = m.group(1).strip().lower()
            cat_map = {
                'frog': 'amphibian', 'toad': 'amphibian', 'salamander': 'amphibian',
                'newt': 'amphibian', 'caecilian': 'amphibian',
                'snake': 'reptile', 'lizard': 'reptile', 'turtle': 'reptile',
                'crocodile': 'reptile', 'alligator': 'reptile', 'tortoise': 'reptile',
                'gecko': 'reptile', 'iguana': 'reptile', 'chameleon': 'reptile',
                'komodo dragon': 'reptile',
                'whale': 'mammal', 'dolphin': 'mammal', 'porpoise': 'mammal',
                'human': 'mammal', 'dog': 'mammal', 'cat': 'mammal',
                'lion': 'mammal', 'tiger': 'mammal', 'bear': 'mammal',
                'elephant': 'mammal', 'giraffe': 'mammal', 'bat': 'mammal',
                'kangaroo': 'mammal', 'platypus': 'mammal', 'seal': 'mammal',
                'walrus': 'mammal', 'otter': 'mammal', 'rat': 'mammal',
                'mouse': 'mammal', 'rabbit': 'mammal', 'horse': 'mammal',
                'cow': 'mammal', 'pig': 'mammal', 'sheep': 'mammal', 'goat': 'mammal',
                'monkey': 'mammal', 'ape': 'mammal', 'gorilla': 'mammal',
                'chimpanzee': 'mammal', 'orangutan': 'mammal',
                'salmon': 'fish', 'tuna': 'fish', 'trout': 'fish', 'cod': 'fish',
                'shark': 'fish', 'goldfish': 'fish', 'bass': 'fish', 'catfish': 'fish',
                'clownfish': 'fish', 'piranha': 'fish', 'eel': 'fish',
                'eagle': 'bird', 'hawk': 'bird', 'owl': 'bird', 'sparrow': 'bird',
                'robin': 'bird', 'pigeon': 'bird', 'parrot': 'bird', 'crow': 'bird',
                'raven': 'bird', 'flamingo': 'bird', 'penguin': 'bird',
                'ostrich': 'bird', 'chicken': 'bird', 'duck': 'bird', 'turkey': 'bird',
                'peacock': 'bird', 'woodpecker': 'bird', 'hummingbird': 'bird',
                'swan': 'bird', 'goose': 'bird', 'seagull': 'bird',
                'spider': 'arachnid', 'scorpion': 'arachnid', 'tick': 'arachnid',
                'mite': 'arachnid', 'daddy longlegs': 'arachnid',
                'ant': 'insect', 'bee': 'insect', 'wasp': 'insect', 'butterfly': 'insect',
                'moth': 'insect', 'fly': 'insect', 'mosquito': 'insect',
                'beetle': 'insect', 'ladybug': 'insect', 'cricket': 'insect',
                'grasshopper': 'insect', 'dragonfly': 'insect', 'cockroach': 'insect',
                'termite': 'insect', 'flea': 'insect', 'louse': 'insect',
                'oak': 'tree', 'pine': 'tree', 'maple': 'tree', 'birch': 'tree',
                'willow': 'tree', 'palm': 'tree', 'bamboo': 'grass',
                'rose': 'flower', 'daisy': 'flower', 'tulip': 'flower',
                'sunflower': 'flower', 'lily': 'flower', 'orchid': 'flower',
                'diamond': 'gem', 'ruby': 'gem', 'sapphire': 'gem', 'emerald': 'gem',
                'opal': 'gem', 'topaz': 'gem', 'amethyst': 'gem', 'garnet': 'gem',
                'gold': 'metal', 'silver': 'metal', 'iron': 'metal', 'copper': 'metal',
                'aluminum': 'metal', 'steel': 'alloy', 'bronze': 'alloy', 'brass': 'alloy',
                'oxygen': 'gas', 'nitrogen': 'gas', 'hydrogen': 'gas', 'helium': 'gas',
                'neon': 'gas', 'argon': 'gas',
                'water': 'liquid', 'oil': 'liquid', 'mercury': 'liquid',
                'ice': 'solid', 'rock': 'solid', 'wood': 'solid',
                'piano': 'instrument', 'guitar': 'instrument', 'violin': 'instrument',
                'drum': 'instrument', 'flute': 'instrument', 'trumpet': 'instrument',
                'saxophone': 'instrument', 'harp': 'instrument', 'cello': 'instrument',
                'clarinet': 'instrument', 'trombone': 'instrument',
                'car': 'vehicle', 'truck': 'vehicle', 'bus': 'vehicle', 'train': 'vehicle',
                'airplane': 'vehicle', 'boat': 'vehicle', 'ship': 'vehicle',
                'bicycle': 'vehicle', 'motorcycle': 'vehicle', 'helicopter': 'vehicle',
                'submarine': 'vehicle', 'scooter': 'vehicle',
                'apple': 'fruit', 'banana': 'fruit', 'orange': 'fruit', 'grape': 'fruit',
                'mango': 'fruit', 'pineapple': 'fruit', 'watermelon': 'fruit',
                'strawberry': 'fruit', 'blueberry': 'fruit', 'raspberry': 'fruit',
                'blackberry': 'fruit', 'cherry': 'fruit', 'peach': 'fruit',
                'pear': 'fruit', 'plum': 'fruit', 'kiwi': 'fruit',
                'lemon': 'fruit', 'lime': 'fruit', 'grapefruit': 'fruit',
                'tomato': 'fruit', 'avocado': 'fruit', 'cucumber': 'fruit',
                'carrot': 'vegetable', 'broccoli': 'vegetable', 'spinach': 'vegetable',
                'lettuce': 'vegetable', 'cabbage': 'vegetable', 'celery': 'vegetable',
                'potato': 'vegetable', 'onion': 'vegetable', 'garlic': 'vegetable',
                'pea': 'vegetable', 'corn': 'vegetable', 'pepper': 'vegetable',
                'mushroom': 'fungus', 'yeast': 'fungus', 'mold': 'fungus',
                'chicken': 'poultry', 'beef': 'meat', 'pork': 'meat', 'lamb': 'meat',
                'rice': 'grain', 'wheat': 'grain', 'oats': 'grain', 'barley': 'grain',
                'rye': 'grain', 'corn grain': 'grain', 'quinoa': 'grain',
            }
            for obj, cat in cat_map.items():
                if obj in item or item in obj:
                    log(f"[Accessibility] Type solver: '{item}' is a {cat}")
                    return cat

        # ── WHAT IS X CALLED WHEN Y: "What is a young cat called?" ──
        m = re.search(
            r'what\s+(?:is|are|do\s+you|do\s+we)\s+(?:a\s+|an\s+|the\s+)?'
            r'(?:young|baby|child|adult|male|female|group|collective)',
            t, re.IGNORECASE)
        if m:
            full = orig.strip().lower()
            called_map = {
                'young cat': 'kitten', 'baby cat': 'kitten',
                'young dog': 'puppy', 'baby dog': 'puppy',
                'young cow': 'calf', 'baby cow': 'calf',
                'young horse': 'foal', 'baby horse': 'foal',
                'young chicken': 'chick', 'baby chicken': 'chick',
                'young duck': 'duckling', 'baby duck': 'duckling',
                'young goose': 'gosling', 'baby goose': 'gosling',
                'young sheep': 'lamb', 'baby sheep': 'lamb',
                'young goat': 'kid', 'baby goat': 'kid',
                'young pig': 'piglet', 'baby pig': 'piglet',
                'young bear': 'cub', 'baby bear': 'cub',
                'young lion': 'cub', 'baby lion': 'cub',
                'young tiger': 'cub', 'baby tiger': 'cub',
                'young wolf': 'pup', 'baby wolf': 'pup',
                'young fox': 'kit', 'baby fox': 'kit',
                'young rabbit': 'kit', 'baby rabbit': 'kit',
                'young deer': 'fawn', 'baby deer': 'fawn',
                'young kangaroo': 'joey', 'baby kangaroo': 'joey',
                'young frog': 'tadpole', 'baby frog': 'tadpole',
                'young fish': 'fry', 'baby fish': 'fry',
                'young eel': 'elver', 'baby eel': 'elver',
                'young swan': 'cygnet', 'baby swan': 'cygnet',
                'young eagle': 'eaglet', 'baby eagle': 'eaglet',
                'young owl': 'owlet', 'baby owl': 'owlet',
                'young pigeon': 'squab', 'baby pigeon': 'squab',
                'young snake': 'snakelet', 'baby snake': 'snakelet',
                'young elephant': 'calf', 'baby elephant': 'calf',
                'young whale': 'calf', 'baby whale': 'calf',
                'young dolphin': 'calf', 'baby dolphin': 'calf',
                'young seal': 'pup', 'baby seal': 'pup',
                'young otter': 'pup', 'baby otter': 'pup',
                'young beaver': 'kit', 'baby beaver': 'kit',
                'adult male chicken': 'rooster',
                'adult female chicken': 'hen',
                'adult male cow': 'bull',
                'adult female cow': 'cow',
                'adult male horse': 'stallion',
                'adult female horse': 'mare',
                'adult male sheep': 'ram',
                'adult female sheep': 'ewe',
                'adult male pig': 'boar',
                'adult female pig': 'sow',
                'adult male deer': 'buck',
                'adult female deer': 'doe',
                'group of wolf': 'pack',
                'group of fish': 'school',
                'group of bird': 'flock',
                'group of sheep': 'flock',
                'group of cattle': 'herd',
                'group of lion': 'pride',
                'group of bee': 'swarm',
                'group of ant': 'colony',
                'group of dolphin': 'pod',
                'group of whale': 'pod',
                'group of monkey': 'troop',
                'group of geese': 'gaggle',
                'group of crow': 'murder',
                'group of owl': 'parliament',
                'group of elephant': 'herd',
            }
            for obj, ans in called_map.items():
                if obj in full:
                    log(f"[Accessibility] Called solver: '{obj}' → {ans}")
                    return ans

        # ── UNIVERSAL NUMBER SUM: last-resort fallback ──
        # If the question has 2+ numbers and nothing else matched,
        # just sum them all. Catches any jar/coin/math word problem
        # the specific solvers might have missed.
        all_nums = re.findall(r'\b(\d+)\b', orig)
        if len(all_nums) >= 2:
            total = sum(int(n) for n in all_nums)
            log(f"[Accessibility] Universal number sum: {'+'.join(all_nums)} = {total}")
            return str(total)

        return None

    _reject_counts: dict = {}
    _mistakes_made = 0

    def _human_typo(answer: str) -> str:
        """Introduce one plausible human typo into a short answer."""
        a = (answer or "").strip()
        if len(a) < 2:
            return answer
        # Most common human typo: two adjacent letters swapped.
        if " " not in a and a.isalpha():
            i = random.randrange(len(a) - 1)
            if a[i].lower() != a[i + 1].lower():
                return a[:i] + a[i + 1] + a[i] + a[i + 2:]
        # Fallback: duplicate one character.
        i = random.randrange(len(a))
        return a[:i] + a[i] + a[i:]

    async def _get_answer(hcaptcha, q: int) -> Optional[str]:
        """Get the answer with 3 layers:
        Layer 1: regex patterns (513)   — exact phrasings
        Layer 2: semantic topic table   — any phrasing containing topics
        Layer 3: LLM fallback           — ANY unknown question → Ollama / OpenAI-compatible"""
        text = await _read_question_text()
        log(f"[Accessibility] Q{q} text: '{text[:200]}'")
        raw = text

        # ── Clean raw frame text into a bare question ──
        # Raw text often arrives as: "Answer the following question with a
        # single word, number, or phrase. Can bread be stored frozen? Please
        # try again. ⚠️ Verify EN". Strip the instruction prefix, the
        # "please try again" trailer, and emoji junk so the solvers (yes/no,
        # jar, KB...) see the bare question and answer instantly + correctly.
        if text:
            text = re.sub(
                r'(?:please\s+)?answer\s+the\s+following\s+question\s+with\s+a\s+single\s+word'
                r'(?:,\s*number,\s*or\s+phrase)?\.?\s*'
                r'|please\s+use\s+a\s+single\s+word(?:,\s*number,\s*or\s+phrase)?'
                r'\s+in\s+your\s+answer\s+to\s+the\s+following\s+question\.?\s*'
                r'|please\s+answer\s+the\s+following\s+question\.?\s*'
                r'|(?:please\s+)?answer\s+the\s+following\s+question\.?\s*',
                '', text, flags=re.IGNORECASE
            )
            text = re.sub(r'please\s+try\s+again.*', '', text,
                          flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r'\s*\u26a0\ufe0f\s*', ' ', text)
            text = re.sub(r'\s*(?:verify|skip)\s+en\b[^\w]*$', '', text,
                          flags=re.IGNORECASE)
            text = re.sub(r'\s+', ' ', text).strip(' .?|:;-')

        # ── "Please try again" = the previous answer was rejected. The SAME
        # question stays on screen — retry it instead of skipping. Capped at
        # 2 attempts per question so a truly unanswerable one gets skipped
        # rather than looping forever. ──
        if raw and re.search(r'please\s+try\s+again', raw, re.IGNORECASE):
            if not text:
                log(f"[Accessibility] Q{q} rejected but no question text left — skipping",
                    level="warn")
                return None
            _rkey = re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()[:80]
            _reject_counts[_rkey] = _reject_counts.get(_rkey, 0) + 1
            if _reject_counts[_rkey] >= 2:
                log(f"[Accessibility] Q{q} rejected twice — skipping this question",
                    level="warn")
                return None
            log(f"[Accessibility] Q{q} answer was rejected — retrying: '{text[:120]}'",
                level="warn")

        # ---- Non-text instruction questions (drag/slide/image-pick) ----
        # These are GESTURE or IMAGE challenges the text input cannot answer.
        # Detect and skip INSTANTLY - calling the LLM on them just burns the
        # full timeout ('Drag the animal into the correct silhouette' cost
        # 2x22s x2 attempts = 88s of wasted LLM time in one run).
        if text:
            _tl = text.lower()
            if (re.search(r'\b(drag|silhouette|slider)\b', _tl)
                    or re.search(r'\b(select|click|pick|choose)\b.*\b(images?|pictures?|photos?)\b', _tl)
                    or re.search(r'\b(images?|pictures?|photos?)\b.*\b(select|click|pick|choose)\b', _tl)
                    or re.search(r'\bwhich (?:image|picture|photo)\b', _tl)
                    or re.search(r'\b(draw|trace|swipe|rotate)\b', _tl)
                    or re.search(r'\bcomplete (?:the|this) (?:pattern|shape|puzzle)\b', _tl)):
                log(f"[Accessibility] Q{q} non-text instruction ('drag/silhouette' style) - trying VISION solver")
                if await _solve_gesture_challenge(text):
                    log(f"[Accessibility] Q{q} gesture solved by vision model")
                    return "__GESTURE__"
                log(f"[Accessibility] Q{q} vision gesture failed - skipping", level="warn")
                return None

        if text:
            # ── Layers 1+2: local KB ──
            local = _solve_text_question(text)
            if local is not None:
                log(f"[Accessibility] Q{q} solved locally: {local}")
                return local

            # ── Layer 3: LLM fallback (Ollama text model) ──
            # The question TEXT is already extracted — send it straight to
            # Ollama /api/chat as text (no screenshot, no vision model).
            # Hard timeout so a slow model can't stall the captcha.
            log(f"[Accessibility] Q{q} UNKNOWN question (no local match) — calling Layer 3 LLM")
            ans = await _llm_answer_question(text, timeout=12.0)
            if ans:
                log(f"[Accessibility] Q{q} Layer 3 LLM answered: {ans}")
                return ans
            log(f"[Accessibility] Q{q} Layer 3 could not answer either", level="warn")
            # ── Layer 4: VISION model on the real challenge screenshot ──
            # Some questions render as an image with no alt/aria text - a
            # VL model sees the actual pixels and answers exactly.
            if ollama_vision_model:
                try:
                    _shot = await _screenshot_b64(page)
                    if _shot:
                        _vraw = await _llm_answer_vision(
                            _shot,
                            _VISION_PROMPT + "\n\nQuestion text: " + (text or "")[:220],
                            timeout=20.0)
                        _plan = _parse_vision_json(_vraw)
                        if _plan and _plan.get("type") == "text" and _plan.get("answer"):
                            _vans = _clean_llm_answer(str(_plan["answer"]))
                            if _vans:
                                log(f"[Accessibility] Q{q} VISION model answered: {_vans}")
                                return _vans
                except Exception:
                    pass
        else:
            log(f"[Accessibility] Q{q} NO TEXT FOUND — cannot ask LLM without text", level="warn")
        return None

    async def _human_type(hcaptcha, answer: str) -> bool:
        """Human-like typing: pointer-interact with the field, focus it, then
        type character-by-character with real key events and human cadence.
        Instant ``value=`` + dispatch is a textbook bot signature."""
        if not answer:
            return False
        focus_js = (
            "() => {"
            "const inputs = document.querySelectorAll("
            "'input:not([type=\"hidden\"]), textarea, [role=\"textbox\"], [contenteditable=\"true\"]');"
            "for (const inp of inputs) {"
            "if (inp.offsetParent !== null) {"
            "inp.scrollIntoView({ block: 'center' });"
            "const r = inp.getBoundingClientRect();"
            "const cx = r.left + r.width / 2, cy = r.top + r.height / 2;"
            "const ev = (t, x, y) => inp.dispatchEvent(new PointerEvent(t, {"
            "bubbles: true, cancelable: true, clientX: x, clientY: y,"
            "pointerId: 1, pointerType: 'mouse', isPrimary: true,"
            "button: 0, buttons: (t === 'pointerup' ? 0 : 1)}));"
            "ev('pointermove', cx - 16, cy + 3);"
            "ev('pointermove', cx - 5, cy + 1);"
            "ev('pointerdown', cx, cy);"
            "ev('pointerup', cx, cy);"
            "inp.focus();"
            "try { inp.value = ''; inp.dispatchEvent(new Event('input', { bubbles: true })); } catch (e) {}"
            "return 'ok:' + inp.tagName;"
            "}"
            "}"
            "return null;"
            "}"
        )
        try:
            focused = await _challenge_js(focus_js)
        except Exception as e:
            log(f"[Accessibility] human focus error: {e}", level="warn")
            return False
        if not (focused and "ok" in str(focused)):
            return False

        # A human has to visually locate the field before typing.
        await asyncio.sleep(random.uniform(0.45, 0.95))

        def _char_js(ch: str) -> str:
            c = json.dumps(ch)
            return (
                "() => {"
                "const inputs = document.querySelectorAll("
                "'input:not([type=\"hidden\"]), textarea, [role=\"textbox\"], [contenteditable=\"true\"]');"
                "for (const inp of inputs) {"
                "if (inp.offsetParent !== null) {"
                "const ch = " + c + ";"
                "const code = 'Key' + ch.toUpperCase();"
                "inp.dispatchEvent(new KeyboardEvent('keydown', { key: ch, code: code, bubbles: true, cancelable: true }));"
                "if (inp.tagName === 'INPUT' || inp.tagName === 'TEXTAREA') {"
                "const s = (inp.selectionStart != null) ? inp.selectionStart : inp.value.length;"
                "inp.value = inp.value.slice(0, s) + ch + inp.value.slice(s);"
                "inp.selectionStart = inp.selectionEnd = s + 1;"
                "} else {"
                "try { document.execCommand('insertText', false, ch); } catch (e) {}"
                "}"
                "inp.dispatchEvent(new InputEvent('input', { data: ch, inputType: 'insertText', bubbles: true }));"
                "inp.dispatchEvent(new KeyboardEvent('keyup', { key: ch, code: code, bubbles: true, cancelable: true }));"
                "return 'ok';"
                "}"
                "}"
                "return null;"
                "}"
            )

        ok = True
        for ch in answer:
            try:
                res = await _challenge_js(_char_js(ch))
                if not (res and "ok" in str(res)):
                    ok = False
                    break
            except Exception:
                ok = False
                break
            delay = random.uniform(0.045, 0.14)
            if random.random() < 0.08:
                delay += random.uniform(0.15, 0.45)
            await asyncio.sleep(delay)
        if not ok:
            return False

        await asyncio.sleep(random.uniform(0.2, 0.5))
        await _challenge_js(
            "() => { const i = document.querySelector('input:not([type=\"hidden\"]), textarea, [role=\"textbox\"]'); "
            "if (i) { i.dispatchEvent(new Event('change', { bubbles: true })); return 'ok'; } return null; }"
        )
        return True

    async def _type_answer(hcaptcha, answer: str) -> bool:
        """Type answer into the hCaptcha accessibility input.
        Human typing first; instant JS injection kept as a robustness fallback."""
        # Primary: human typing (per-char key events + realistic cadence).
        if await _human_type(hcaptcha, answer):
            log(f"[Accessibility] Human-typed '{answer}'")
            return True

        escaped = answer.replace("\\", "\\\\").replace("'", "\\'")

        # Fallback 0: JS injection - find visible input, set value, fire events
        js_set = (
            "() => {"
            "const inputs = document.querySelectorAll("
            "'input:not([type=\"hidden\"]), textarea, [role=\"textbox\"], [contenteditable=\"true\"]'"
            ");"
            "for (const inp of inputs) {"
            "if (inp.offsetParent !== null) {"
            "inp.focus();"
            "inp.value = '';"
            "inp.value = '" + escaped + "';"
            "inp.dispatchEvent(new Event('input', { bubbles: true }));"
            "inp.dispatchEvent(new Event('change', { bubbles: true }));"
            "return 'ok:' + inp.tagName;"
            "}"
            "}"
            "return null;"
            "}"
        )
        try:
            result = await _challenge_js(js_set)
            if result and 'ok' in str(result):
                log(f"[Accessibility] JS-set '{answer}' ({result})")
                return True
        except Exception as e:
            log(f"[Accessibility] JS-set error: {e}")

        # Fallback 1: get_by_role (works on newer Playwright)
        try:
            inp = hcaptcha.get_by_role("textbox", name="Challenge Text Input").first
            await inp.wait_for(state="visible", timeout=3000)
            await inp.click()
            await inp.fill("")
            await inp.type(answer, delay=30)
            log(f"[Accessibility] Typed '{answer}' via get_by_role")
            return True
        except Exception:
            pass

        # Fallback 2: keyboard type (click visible input first)
        try:
            inp = hcaptcha.locator(
                "input:not([type='hidden']), textarea, [role='textbox']"
            ).first
            await inp.click()
            await asyncio.sleep(0.2)
            await page.keyboard.press("Control+a")
            await page.keyboard.type(answer, delay=50)
            log(f"[Accessibility] Typed '{answer}' via keyboard")
            return True
        except Exception:
            pass

        # Fallback 3: brute-force page-level keyboard
        try:
            await page.keyboard.type(answer, delay=50)
            log(f"[Accessibility] Typed '{answer}' via brute-force keyboard")
            return True
        except Exception:
            pass

        return False

    async def _submit_answer(hcaptcha) -> bool:
        """Click Next / Submit on the accessibility challenge.
        Primary: JS injection via _challenge_js (works on nested iframes).
        The 1.5s wait avoids the Skip button which shares coordinates."""
        log("[Accessibility] Waiting 0.6s before clicking Next (avoid Skip)")
        await asyncio.sleep(0.6)

        # ── Primary: JS injection — click the visible action button ──
        js_click = r"""() => {
            const names = ['Next', 'Submit', 'Verify', 'Continue', 'OK', 'Done'];
            const btns = document.querySelectorAll('button, [role="button"]');
            for (const b of btns) {
                const txt = (b.textContent || '').trim().toLowerCase();
                const label = ((b.getAttribute('aria-label') || '') + ' ' + txt).toLowerCase();
                for (const n of names) {
                    if (label.includes(n.toLowerCase()) && b.offsetParent !== null) {
                        b.click();
                        return 'clicked:' + n;
                    }
                }
            }
            // Fallback: any visible primary submit button
            for (const b of btns) {
                const t = (b.getAttribute('type') || '').toLowerCase();
                if (t === 'submit' && b.offsetParent !== null) {
                    b.click();
                    return 'clicked:submit';
                }
            }
            return null;
        }"""
        try:
            result = await _challenge_js(js_click)
            if result and 'clicked' in str(result):
                log(f"[Accessibility] Submitted via JS click ({result})")
                return True
        except Exception as e:
            log(f"[Accessibility] JS click error: {e}")

        # ── Fallback 1: get_by_role ──
        for name in ("Next", "Submit", "Verify", "Continue", "OK"):
            try:
                btn = hcaptcha.get_by_role("button", name=name).first
                await btn.wait_for(state="visible", timeout=2000)
                await btn.click(timeout=2000)
                log(f"[Accessibility] Submitted via get_by_role {name}")
                return True
            except Exception:
                continue

        # ── Fallback 2: selector-based (aria-label + type) ──
        for btn_sel in [
            'button[aria-label="Next"]',
            'button[aria-label="Submit"]',
            'button[aria-label="Verify"]',
            'button[type="button"]',
            'button[type="submit"]',
            'button:has-text("Next")',
            'button:has-text("Submit")',
            'button:has-text("Verify")',
            'button:has-text("OK")',
            'button:has-text("Continue")',
        ]:
            try:
                await hcaptcha.locator(btn_sel).first.click(timeout=2000)
                log(f"[Accessibility] Submitted via {btn_sel}")
                return True
            except Exception:
                pass

        # ── Fallback 3: Enter key ──
        try:
            inp = hcaptcha.locator("input:not([type='hidden']), textarea").first
            await inp.press("Enter", timeout=2000)
            log("[Accessibility] Submitted via Enter")
            return True
        except Exception:
            pass

        # ── Fallback 4: coordinate click on iframe body ──
        # Next button is reliably at the bottom-right of the iframe
        try:
            btn = hcaptcha.locator(
                'button[aria-label="Next"], button:has-text("Next")'
            ).first
            box = await btn.bounding_box()
            if box:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                await page.mouse.click(cx, cy)
                log(f"[Accessibility] Submitted via coordinate click ({int(cx)},{int(cy)})")
                return True
        except Exception:
            pass
        # Ultimate fallback: click frame body bottom-right
        try:
            box = await hcaptcha.locator("body").first.bounding_box()
            if box:
                cx = box["x"] + box["width"] - 60
                cy = box["y"] + box["height"] - 40
                await page.mouse.click(cx, cy)
                log("[Accessibility] Submitted via frame bottom-right click")
                return True
        except Exception:
            pass

        return False


    # ── Main flow ─────────────────────────────────────────

    try:
        # ── Step 1: Locate the hCaptcha challenge iframe via frame_locator ──
        # Use the passed iframe element to build a reliable frame_locator, or
        # fall back to searching the page for the challenge iframe.
        hcaptcha = None
        if iframe is not None:
            # Derive frame_locator from the iframe element that server.py found
            try:
                frame_url = await iframe.get_attribute("src")
                if frame_url and "hcaptcha.com" in frame_url:
                    hcaptcha = page.frame_locator(f'iframe[src="{frame_url}"]')
                    log("[Accessibility] Using passed iframe for frame_locator")
            except Exception:
                pass
        if hcaptcha is None:
            HCAPTCHA_FRAME = 'iframe[title="hCaptcha challenge"]'
            hcaptcha = page.frame_locator(HCAPTCHA_FRAME)

        # Verify the iframe body is attached (rendered)
        try:
            await hcaptcha.locator("body").first.wait_for(
                state="attached", timeout=15000
            )
            log("[Accessibility] hCaptcha challenge iframe located via frame_locator")
        except Exception:
            log("[Accessibility] hCaptcha challenge iframe not found via frame_locator — "
                "trying fallback selectors", level="warn")
            # Fallback: try other iframe selectors
            for fallback_sel in [
                'iframe[src*="hcaptcha.com/captcha"]',
                'iframe[src*="hcaptcha.com"]',
                'iframe[title*="hCaptcha"]',
            ]:
                try:
                    hcaptcha = page.frame_locator(fallback_sel)
                    await hcaptcha.locator("body").first.wait_for(
                        state="attached", timeout=5000
                    )
                    log(f"[Accessibility] Found via fallback: {fallback_sel}")
                    break
                except Exception:
                    continue
            else:
                log("[Accessibility] Cannot locate any hCaptcha iframe — aborting",
                    level="error")
                return False

        for attempt in range(1, max_attempts + 1):
            log(f"[Accessibility] Attempt {attempt}/{max_attempts}")

            if await _token_present():
                # Token present, but a NEW challenge can still be showing
                # (hCaptcha chains captchas). Only bail if there's no
                # challenge iframe right now.
                try:
                    _chall = page.locator(
                        'iframe[title="hCaptcha challenge"], '
                        'iframe[src*="hcaptcha-challenge"]'
                    )
                    _new_chall = await _chall.count() > 0
                except Exception:
                    _new_chall = False
                if not _new_chall:
                    log("[Accessibility] [OK] Already solved — token present!")
                    return True
                log("[Accessibility] Token present but NEW challenge detected — solving it")

            # Always open the accessibility challenge via the menu.
            # NOTE: removed the "already active" shortcut because
            # _accessibility_active was false-matching hidden inputs in
            # the hCaptcha token field. Always clicking 3-dots is safer.
            if not await _open_accessibility_challenge(hcaptcha):
                log("[Accessibility] Could not open accessibility challenge",
                    level="warn")
                await asyncio.sleep(0.8)
                continue

            # ── Captcha-chain loop: keep solving until iframe disappears ──
            # hCaptcha can throw multiple challenges in a row after each
            # set of accessibility questions. Solve them all.
            chain_attempt = 0
            while True:
                chain_attempt += 1
                if chain_attempt > 4:
                    log("[Accessibility] Too many captcha chains — aborting",
                        level="warn")
                    break
                if chain_attempt > 1:
                    log(f"[Accessibility] NEW captcha detected — chain #{chain_attempt}")
                    # Re-open accessibility for the new captcha
                    if not await _open_accessibility_challenge(hcaptcha):
                        log("[Accessibility] Could not re-open accessibility for chain",
                            level="warn")
                        break
                    await asyncio.sleep(0.8)

                # ── Answer every question in this chain ──
                # Track recent Q texts & answers to detect infinite loops
                # (same non-question text being answered repeatedly)
                _prev_texts = []
                _prev_answers = []
                for q in range(1, max_questions + 1):
                    if await _token_present():
                        log("[Accessibility] Token appeared mid-chain!")
                        break

                    # ── Pure error-state guard ──
                    # hCaptcha shows "Please try again" with NO question text
                    # when the previous attempt was rejected/errored. Burning
                    # Q1..Q6 on it is fake solving — abort the chain so the
                    # outer attempt loop retries fresh instead.
                    try:
                        _body_txt = await hcaptcha.locator("body").inner_text()
                    except Exception:
                        _body_txt = ""
                    _low_txt = (_body_txt or "").lower()
                    if "please try again" in _low_txt:
                        _clean_txt = re.sub(
                            r"please\s+try\s+again.*", "", _low_txt,
                            flags=re.DOTALL)
                        _clean_txt = re.sub(
                            r"\s*(?:verify|skip)\s*en\b.*$", "", _clean_txt)
                        _clean_txt = re.sub(
                            r"[^a-z0-9?]+", " ", _clean_txt).strip()
                        if len(_clean_txt) < 6:
                            log("[Accessibility] hCaptcha error state ('Please try again', no question) — aborting chain", level="warn")
                            break

                    answer = await _get_answer(hcaptcha, q)
                    if answer == "__GESTURE__":
                        # Vision model already performed the drag/click
                        # gesture - no typing/submitting needed. Wait for
                        # the challenge to advance or a token to appear.
                        log(f"[Accessibility] Q{q} gesture performed - waiting for challenge to advance")
                        for _ti in range(8):
                            if await _token_present():
                                break
                            await asyncio.sleep(0.3)
                        if await _token_present():
                            log(f"[Accessibility] Token appeared after Q{q} (gesture)!")
                            break
                        continue
                    # ── Duplicate detection: if we get the same question
                    # text 3+ times in a row, the page isn't showing real
                    # captcha challenges — abort this chain.
                    if answer:
                        cur_text = await _read_question_text()
                        _prev_texts.append(cur_text[:200])
                        _prev_answers.append(answer)
                        if len(_prev_texts) >= 3:
                            unique_texts = set(_prev_texts[-3:])
                            unique_ans = set(a for a in _prev_answers[-3:] if a)
                            if len(unique_texts) <= 1 and len(unique_ans) <= 1:
                                log("[Accessibility] Same question+answer repeated 3x — "
                                    "page isn't showing real challenges, aborting chain",
                                    level="warn")
                                break
                    if answer is None:
                        log(f"[Accessibility] Q{q}: No answer — waiting 0.6s then clicking Skip", level="warn")
                        await asyncio.sleep(0.6)
                        try:
                            skip_result = await _challenge_js(
                                r"""() => {
                                    const btns = document.querySelectorAll('button, [role="button"]');
                                    for (const b of btns) {
                                        const txt = ((b.textContent || '') + ' ' + (b.getAttribute('aria-label') || '')).toLowerCase().trim();
                                        if (txt.includes('skip') && b.offsetParent !== null) {
                                            b.click();
                                            return 'clicked_skip';
                                        }
                                    }
                                    return null;
                                }"""
                            )
                            if skip_result:
                                log(f"[Accessibility] Q{q} skipped via JS click")
                            else:
                                log(f"[Accessibility] Q{q} skip button not found — moving to next question anyway")
                        except Exception as e:
                            log(f"[Accessibility] Q{q} skip error: {e}")
                        await asyncio.sleep(0.6)
                        continue

                    # Human imperfection: occasionally a real user mistypes,
                    # hCaptcha shows "please try again", and they correct it.
                    # Reproducing that (once per challenge) breaks the
                    # machine-perfect signature that flags the session.
                    if (answer and answer != "__GESTURE__"
                            and len(answer) >= 2 and _mistakes_made == 0
                            and random.random() < HUMAN_MISTAKE_RATE):
                        answer = _human_typo(answer)
                        _mistakes_made += 1
                        log(f"[Accessibility] Q{q} humanized typo -> '{answer}'")

                    log(f"[Accessibility] Q{q} solved: {answer}")

                    # Human think-time — a real user reads the question first.
                    await asyncio.sleep(random.uniform(HUMAN_THINK_MIN, HUMAN_THINK_MAX))

                    if not await _type_answer(hcaptcha, answer):
                        log("[Accessibility] Could not type answer", level="warn")
                        break

                    await asyncio.sleep(random.uniform(0.6, 1.1))

                    if not await _submit_answer(hcaptcha):
                        log("[Accessibility] Could not submit", level="warn")
                        break

                    # Wait for Next→new question transition. Poll the token
                    # so a COMPLETED captcha returns immediately instead of
                    # always costing a fixed 2s.
                    for _ti in range(7):
                        if await _token_present():
                            break
                        await asyncio.sleep(0.3)

                    # Check if token appeared (captcha complete)
                    if await _token_present():
                        log(f"[Accessibility] Token appeared after Q{q}!")
                        break

                # ── After answering all questions, detect ANY new captcha ──
                # hCaptcha can throw a NEW challenge right after a solved one
                # (Discord re-arms the widget). The widget iframe
                # (newassets.hcaptcha.com) stays in the DOM forever, so only
                # watch the CHALLENGE iframe — otherwise we'd loop forever on
                # the idle widget.
                # Phase 1: let the solved challenge CLOSE (hCaptcha collapses
                # the challenge iframe on success) — up to 4s. Phase 2: watch
                # for a fresh rendered challenge to appear — up to 8s. A
                # chained captcha takes a couple of seconds to spawn, and the
                # old single 0.5s check missed it.
                chall_sel = ('iframe[title="hCaptcha challenge"], '
                             'iframe[src*="hcaptcha-challenge"]')
                _chall = page.locator(chall_sel)

                # Phase 1: wait for the solved challenge to close.
                saw_absent = False
                for _pi in range(8):
                    try:
                        _cnt = await _chall.count()
                    except Exception:
                        _cnt = 1
                    if _cnt == 0:
                        saw_absent = True
                        break
                    await asyncio.sleep(0.5)

                # Phase 2: poll for a NEW rendered challenge. Only chain when
                # the iframe genuinely (re)appeared — a stale solved frame
                # lingering in the DOM must not count.
                new_chall_seen = False
                for _pi in range(16):
                    try:
                        _cnt = await _chall.count()
                    except Exception:
                        _cnt = 0
                    if _cnt == 0:
                        saw_absent = True
                    else:
                        try:
                            _box = await _chall.first.bounding_box()
                        except Exception:
                            _box = None
                        _rendered = _box is None or _box.get("height", 0) >= 40
                        if _rendered and (saw_absent or not await _token_present()):
                            # Honesty: NEVER chain on a sized-but-blank iframe.
                            # A fresh challenge element is laid out at full size
                            # before its JS paints — verify it actually painted
                            # content (same rule server._challenge_rendered uses).
                            try:
                                _cf = await _chall.first.content_frame()
                                _painted = False
                                if _cf is not None:
                                    _painted = bool(await _cf.evaluate(
                                        """() => {
                                            if (document.readyState !== 'complete') return false;
                                            const b = document.getElementById('hcaptcha-body');
                                            if (b && b.offsetHeight >= 40) return true;
                                            const t = (document.body && document.body.innerText) || '';
                                            return t.trim().length >= 5;
                                        }"""))
                            except Exception:
                                _painted = False
                            if not _painted:
                                await asyncio.sleep(0.5)
                                continue
                            new_chall_seen = True
                            break
                    await asyncio.sleep(0.5)

                if new_chall_seen:
                    # Chain loop below re-opens 3-dots → Accessibility Challenge.
                    log("[Accessibility] NEW captcha detected — clicking 3-dots + accessibility again")
                    continue
                if await _token_present():
                    log("[Accessibility] [OK] No new challenge + token present — done!")
                    return True
                log(f"[Accessibility] No token after Q{q} — more questions or retry")

            log(f"[Accessibility] Attempt {attempt} did not solve — retrying",
                level="warn")
            await asyncio.sleep(0.5)

        log("[Accessibility] [FAIL] Could not solve after all attempts", level="error")
        return False

    except Exception as e:
        log(f"[Accessibility] Fatal error: {e}", level="error")
        import traceback
        traceback.print_exc()
        return False

# Backward-compat: NoCaptchaAI class wrapping the brain solver
# (app.py / server.py still import this)
# ═══════════════════════════════════════════════════════════════

class NoCaptchaAI:
    """Drop-in replacement for the old NoCaptchaAI API client.
    Now uses the trained brains + curl_cffi API flow instead of paid tokens."""

    def __init__(self, log: Optional[Callable] = None):
        self._log = log or (lambda msg, level="info": None)
        self.stats = {"calls": 0, "ok": 0, "failed": 0}

    @property
    def configured(self) -> bool:
        return True  # always ready — no API key needed

    async def solve_hcaptcha(self, sitekey: str, pageurl: str,
                             timeout: float = 85.0, poll: float = 1.0,
                             rqdata: Optional[str] = None) -> Optional[str]:
        """Solve hCaptcha using the brain-based solver. Returns token or None."""
        self.stats["calls"] += 1
        self._log(f"[Solver] hCaptcha (sitekey {sitekey[:12]}...)")
        host = pageurl
        try:
            parsed = __import__("urllib.parse", fromlist=[""]).urlparse(pageurl)
            host = parsed.netloc or parsed.path or pageurl
        except Exception:
            pass
        solver = HCaptchaSolver(sitekey=sitekey, host=host)
        result = await solver.solve()
        if result.get("success"):
            self.stats["ok"] += 1
            token = result.get("token", "")
            self._log(f"[Solver] [OK] Token after {result.get('time', 0):.0f}s")
            return token
        self.stats["failed"] += 1
        self._log(f"[Solver] Failed: {result.get('error')}", level="warn")
        return None

    async def get_balance(self) -> Optional[dict]:
        return {"balance": 0.0, "currency": "USD", "free": True}


async def extract_hcaptcha_rqdata(page) -> str:
    """Pull the hCaptcha Enterprise rqdata from the page (best effort).
    Still exported for backward compat — brain solver doesn't need it."""
    try:
        val = await page.evaluate("""() => {
            const el = document.querySelector('[data-sitekey]');
            if (el) {
                const v = el.getAttribute('data-rqdata') || el.getAttribute('rqdata');
                if (v && v.length > 8) return v;
            }
            for (const s of document.querySelectorAll('script')) {
                const t = s.textContent || '';
                const m = t.match(/"rqdata"\\s*:\\s*"([^"]{8,})"/) ||
                          t.match(/'rqdata'\\s*:\\s*'([^']{8,})'/) ||
                          t.match(/rqdata\\s*[:=]\\s*["']([^"']{8,})["']/);
                if (m) return m[1];
            }
            return '';
        }""")
        if val:
            return str(val).strip()
    except Exception:
        pass
    return ""


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(description="hCaptcha Universal Solver")
    parser.add_argument("--sitekey", default="a9b5fb07-92ff-493f-86fe-352a2803b3df",
                        help="hCaptcha sitekey (default: Discord)")
    parser.add_argument("--host", default="discord.com", help="Target host")
    parser.add_argument("--proxy", help="HTTP proxy URL")
    args = parser.parse_args()

    print("═" * 50)
    print("  hCaptcha Universal Solver — Free Edition")
    print(f"  Sitekey: {args.sitekey}")
    print(f"  Host: {args.host}")
    print("═" * 50)

    solver = HCaptchaSolver(
        sitekey=args.sitekey,
        host=args.host,
        proxy=args.proxy,
    )
    result = await solver.solve()
    if result["success"]:
        print(f"\n✅ Token: {result['token'][:30]}...")
    else:
        print(f"\n❌ Failed: {result.get('error')}")
    print(f"   Time: {result.get('time', 0):.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
