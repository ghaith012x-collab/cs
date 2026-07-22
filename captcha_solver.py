
import asyncio
import base64
import io
import os
import re
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import torch
import open_clip
from PIL import Image
from playwright.async_api import async_playwright, Page, BrowserContext

# --- Configuration --- #
@dataclass
class SolverConfig:
    clip_confidence_threshold: float = 0.55
    max_challenge_rounds: int = 3
    timeout: int = 30  # seconds
    headless: bool = True
    browser_type: str = "chromium"  # chromium, firefox, webkit
    rate_limit_min_delay: float = 0.1  # seconds
    rate_limit_max_delay: float = 0.35  # seconds
    min_solve_time_per_round: float = 2.5 # seconds

# --- CLIP Model Loading --- #
class ClipModel:
    _instance = None

    @classmethod
    async def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
            await cls._instance._load_model()
        return cls._instance

    def __init__(self):
        self.model = None
        self.preprocess = None
        self.tokenizer = None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"

    async def _load_model(self):
        print(f"Loading CLIP model on device: {self.device}")
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="laion2b_s34b_b79k", device=self.device
        )
        self.tokenizer = open_clip.get_tokenizer("ViT-B-32")
        print("CLIP model loaded.")

    async def get_image_features(self, images: List[Image.Image]):
        if self.model is None:
            await self._load_model()
        image_tensors = [self.preprocess(img).unsqueeze(0) for img in images]
        image_input = torch.cat(image_tensors).to(self.device)
        with torch.no_grad():
            image_features = self.model.encode_image(image_input)
        return image_features / image_features.norm(dim=-1, keepdim=True)

    async def get_text_features(self, texts: List[str]):
        if self.model is None:
            await self._load_model()
        text_input = self.tokenizer(texts).to(self.device)
        with torch.no_grad():
            text_features = self.model.encode_text(text_input)
        return text_features / text_features.norm(dim=-1, keepdim=True)

# --- Challenge Detection --- #
class ChallengeDetector:
    def __init__(self, page: Page):
        self.page = page

    async def find_hcaptcha_iframe(self) -> Optional[str]:
        # Check for the hCaptcha iframe by title or src
        iframe_locator = self.page.frame_locator('iframe[src*="hcaptcha.com/captcha"]').or_(
            self.page.frame_locator('iframe[title="hCaptcha security check"]')
        )
        if await iframe_locator.count() > 0:
            return await iframe_locator.get_attribute('src')
        return None

    async def is_captcha_visible(self) -> bool:
        iframe_src = await self.find_hcaptcha_iframe()
        if iframe_src:
            # Check if the challenge itself is visible within the iframe
            # This might need more robust checks depending on hCaptcha's rendering
            try:
                iframe = self.page.frame_locator(f'iframe[src="{iframe_src}"]')
                # Look for common hCaptcha elements like the challenge image or prompt
                challenge_visible = await iframe.locator('.challenge-image').is_visible()
                return challenge_visible
            except Exception:
                return False
        return False

    async def is_solved(self) -> bool:
        iframe_src = await self.find_hcaptcha_iframe()
        if iframe_src:
            iframe = self.page.frame_locator(f'iframe[src="{iframe_src}"]')
            # Check for the 'solved' checkbox or similar indicator
            checkbox = iframe.locator('.checkbox').or_(iframe.locator('#checkbox'))
            if await checkbox.count() > 0:
                return await checkbox.is_checked()
        return False

# --- Solver Base Class --- #
class CaptchaSolver:
    def __init__(self, config: SolverConfig):
        self.config = config
        self.clip_model = None
        self.rate_limit_last_action = 0

    async def _apply_rate_limit(self):
        now = time.time()
        elapsed = now - self.rate_limit_last_action
        delay = random.uniform(self.config.rate_limit_min_delay, self.config.rate_limit_max_delay)
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)
        self.rate_limit_last_action = time.time()

    async def _get_clip_model(self):
        if self.clip_model is None:
            self.clip_model = await ClipModel.get_instance()
        return self.clip_model

    async def solve(self, page: Page) -> bool:
        raise NotImplementedError

    async def close(self):
        pass

# --- Playwright Solver --- #
class PlaywrightSolver(CaptchaSolver):
    def __init__(self, config: SolverConfig):
        super().__init__(config)
        self.browser = None
        self.context: Optional[BrowserContext] = None

    async def _launch_browser(self):
        pw = await async_playwright().start()
        if self.config.browser_type == "chromium":
            self.browser = await pw.chromium.launch(headless=self.config.headless)
        elif self.config.browser_type == "firefox":
            self.browser = await pw.firefox.launch(headless=self.config.headless)
        elif self.config.browser_type == "webkit":
            self.browser = await pw.webkit.launch(headless=self.config.headless)
        else:
            raise ValueError(f"Unsupported browser type: {self.config.browser_type}")

        # Better stealth: navigator props, canvas noise, realistic plugins
        self.context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080},
            java_script_enabled=True,
            accept_downloads=True,
            bypass_csp=True,
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        await self.context.add_init_script("""
            // Stealth: WebGL, Canvas, Plugins, etc.
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbgmofphgfnnbpnkgljmi' },
                { name: 'Native Client', filename: 'internal-nacl-plugin' },
                { name: 'Widevine Content Decryption Module', filename: 'widevinecdm' }
            ]});
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'mimeTypes', { get: () => [
                { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
                { type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format' }
            ]});
            const originalGetParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) { return 'Intel Open Source Technology Center'; }
                if (parameter === 37446) { return 'Mesa DRI Intel(R) HD Graphics 630 (Kaby Lake GT2)'; }
                return originalGetParameter(parameter);
            };
            const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = function() {
                if (this.width === 16 && this.height === 16) {
                    // Add subtle noise to small canvases often used for fingerprinting
                    const ctx = this.getContext('2d');
                    const imageData = ctx.getImageData(0, 0, this.width, this.height);
                    for (let i = 0; i < imageData.data.length; i += 4) {
                        imageData.data[i] += Math.floor(Math.random() * 5) - 2; // Red
                        imageData.data[i + 1] += Math.floor(Math.random() * 5) - 2; // Green
                        imageData.data[i + 2] += Math.floor(Math.random() * 5) - 2; // Blue
                    }
                    ctx.putImageData(imageData, 0, 0);
                }
                return originalToDataURL.apply(this, arguments);
            };
        """)

    async def new_page(self) -> Page:
        if self.browser is None:
            await self._launch_browser()
        return await self.context.new_page()

    async def close(self):
        if self.browser:
            await self.browser.close()

# --- GodSolver (Main Solver Logic) --- #
class GodSolver(CaptchaSolver):
    def __init__(self, config: SolverConfig):
        super().__init__(config)
        self.playwright_solver = PlaywrightSolver(config)
        self.alias_mapping = self._load_alias_mapping()
        self.negative_prompts = [
            "empty background", "sky", "ground", "wall", "nothing",
            "a photo of an empty background", "a photo of the sky",
            "a photo of the ground", "a photo of a wall", "a photo of nothing",
            "an empty background in this image", "the sky in this image",
            "the ground in this image", "a wall in this image", "nothing in this image"
        ]

    def _load_alias_mapping(self) -> Dict[str, List[str]]:
        # Expanded alias mapping for common hCaptcha targets
        return defaultdict(list, {
            "car": ["automobile", "vehicle", "sedan", "coupe", "truck", "van", "bus", "jeep", "pickup"],
            "bus": ["coach", "public transport", "school bus"],
            "truck": ["lorry", "articulated lorry", "pickup truck", "delivery truck"],
            "bicycle": ["bike", "mountain bike", "road bike", "tricycle"],
            "motorcycle": ["motorbike", "scooter", "moped"],
            "traffic light": ["traffic signal", "stop light"],
            "fire hydrant": ["hydrant"],
            "crosswalk": ["zebra crossing", "pedestrian crossing"],
            "bridge": ["overpass", "viaduct"],
            "boat": ["ship", "yacht", "ferry", "vessel"],
            "airplane": ["plane", "aircraft", "jet"],
            "train": ["locomotive", "railway car"],
            "parking meter": ["meter"],
            "chimney": ["smokestack"],
            "palm tree": ["date palm", "coconut tree"],
            "mountain": ["hill", "peak", "summit"],
            "river": ["stream", "creek"],
            "road": ["street", "highway", "avenue"],
            "building": ["house", "apartment", "skyscraper", "structure"],
            "tree": ["plant", "foliage", "bush"],
            "dog": ["puppy", "canine"],
            "cat": ["kitten", "feline"],
            "bird": ["fowl", "avian"],
            "person": ["human", "pedestrian", "figure"],
            "robot": ["android", "bot"],
            "animal": ["creature", "beast"],
            "flower": ["blossom", "bloom"],
            "cloud": ["sky", "cumulus", "stratus"],
            "water": ["ocean", "lake", "sea", "pond"],
            "snow": ["ice", "sleet"],
            "rain": ["drizzle", "shower"],
            "sun": ["sunlight", "star"],
            "moon": ["crescent", "lunar"],
            "star": ["celestial body", "twinkle"],
            "pizza": ["pie", "slice"],
            "sandwich": ["sub", "hoagie"],
            "burger": ["hamburger", "cheeseburger"],
            "hot dog": ["frankfurter", "wiener"],
            "donut": ["doughnut"],
            "coffee": ["espresso", "latte", "cappuccino"],
            "tea": ["green tea", "black tea"],
            "sushi": ["nigiri", "sashimi"],
            "taco": ["burrito", "quesadilla"],
            "book": ["novel", "textbook", "magazine"],
            "computer": ["laptop", "desktop", "pc"],
            "phone": ["smartphone", "mobile phone"],
            "watch": ["wristwatch", "clock"],
            "camera": ["photographic device"],
            "television": ["tv", "monitor"],
            "chair": ["seat", "stool"],
            "table": ["desk", "counter"],
            "lamp": ["light", "lantern"],
            "door": ["entrance", "gate"],
            "window": ["pane", "opening"],
            "cup": ["mug", "glass"],
            "bottle": ["flask", "container"],
            "shoe": ["sneaker", "boot"],
            "hat": ["cap", "beanie"],
            "glove": ["mitten"],
            "sock": ["stocking"],
            "shirt": ["t-shirt", "blouse"],
            "pants": ["trousers", "jeans"],
            "dress": ["gown", "frock"],
            "skirt": ["kilt"],
            "jacket": ["coat", "blazer"],
            "bag": ["purse", "backpack", "handbag"],
            "wallet": ["billfold"],
            "key": ["opener"],
            "coin": ["currency", "token"],
            "gem": ["jewel", "crystal"],
            "ring": ["band", "circlet"],
            "necklace": ["chain", "pendant"],
            "earring": ["stud", "hoop"],
            "bracelet": ["bangle", "cuff"],
            "crown": ["tiara", "diadem"],
            "sword": ["blade", "saber"],
            "shield": ["buckler", "aegis"],
            "bow": ["archery bow"],
            "arrow": ["shaft", "dart"],
            "axe": ["hatchet", "tomahawk"],
            "hammer": ["mallet", "gavel"],
            "wrench": ["spanner"],
            "screwdriver": ["driver"],
            "saw": ["hacksaw", "handsaw"],
            "knife": ["dagger", "cutter"],
            "fork": ["tine"],
            "spoon": ["scoop"],
            "plate": ["dish", "platter"],
            "bowl": ["basin", "tureen"],
            "cup": ["mug", "goblet"],
            "bottle": ["flask", "carafe"],
            "can": ["tin", "container"],
            "box": ["crate", "carton"],
            "basket": ["hamper", "creel"],
            "bag": ["sack", "pouch"],
            "umbrella": ["parasol", "gamp"],
            "guitar": ["acoustic guitar", "electric guitar"],
            "piano": ["keyboard", "grand piano"],
            "violin": ["fiddle"],
            "drum": ["percussion", "snare drum"],
            "trumpet": ["cornet", "bugle"],
            "flute": ["fife", "piccolo"],
            "saxophone": ["sax"],
            "microphone": ["mic", "mike"],
            "speaker": ["loudspeaker", "monitor"],
            "headphone": ["earphone", "headset"],
            "camera": ["camcorder", "webcam"],
            "television": ["display", "screen"],
            "remote control": ["remote", "clicker"],
            "battery": ["cell", "power pack"],
            "charger": ["adapter", "power supply"],
            "plug": ["socket", "connector"],
            "wire": ["cable", "cord"],
            "light bulb": ["bulb", "lamp"],
            "fan": ["ventilator", "blower"],
            "heater": ["radiator", "furnace"],
            "air conditioner": ["ac", "cooler"],
            "refrigerator": ["fridge", "icebox"],
            "microwave": ["micro"],
            "oven": ["stove", "range"],
            "dishwasher": ["dish washer"],
            "washing machine": ["washer", "laundry machine"],
            "dryer": ["tumble dryer"],
            "vacuum cleaner": ["hoover", "vacuum"],
            "broom": ["brush", "sweeper"],
            "mop": ["swab"],
            "bucket": ["pail", "can"],
            "sponge": ["loofah", "scourer"],
            "soap": ["detergent", "cleanser"],
            "shampoo": ["conditioner"],
            "toothbrush": ["tooth brush"],
            "toothpaste": ["tooth paste"],
            "towel": ["cloth", "napkin"],
            "mirror": ["looking glass", "reflector"],
            "comb": ["brush"],
            "hair dryer": ["blow dryer"],
            "razor": ["shaver"],
            "scissors": ["shears"],
            "needle": ["pin", "stylus"],
            "thread": ["yarn", "filament"],
            "button": ["fastener", "switch"],
            "zipper": ["slide fastener"],
            "key": ["fob", "opener"],
            "lock": ["fastening", "clasp"],
            "chain": ["link", "shackle"],
            "rope": ["cord", "cable"],
            "ladder": ["steps", "stairway"],
            "tool box": ["toolbox"],
            "hammer": ["mallet", "gavel"],
            "saw": ["hacksaw", "handsaw"],
            "drill": ["borer", "press"],
            "tape measure": ["measuring tape"],
            "ruler": ["straightedge"],
            "pencil": ["graphite pencil"],
            "pen": ["ballpoint pen", "fountain pen"],
            "paper": ["sheet", "document"],
            "book": ["volume", "tome"],
            "newspaper": ["paper", "journal"],
            "magazine": ["periodical", "glossy"],
            "envelope": ["mailer", "wrapper"],
            "stamp": ["postage stamp"],
            "card": ["greeting card", "playing card"],
            "gift": ["present", "offering"],
            "balloon": ["air balloon", "blimp"],
            "candle": ["taper", "wick"],
            "cake": ["pastry", "gateau"],
            "cookie": ["biscuit", "cracker"],
            "candy": ["sweet", "confectionery"],
            "chocolate": ["cocoa", "bonbon"],
            "ice cream": ["gelato", "sorbet"],
            "juice": ["nectar", "drink"],
            "soda": ["pop", "fizzy drink"],
            "beer": ["ale", "lager"],
            "wine": ["vino", "grape wine"],
            "cocktail": ["mixed drink", "aperitif"],
            "pizza": ["pie", "slice"],
            "burger": ["hamburger", "cheeseburger"],
            "fries": ["chips", "french fries"],
            "salad": ["greens", "coleslaw"],
            "soup": ["broth", "stew"],
            "bread": ["loaf", "bun"],
            "cheese": ["dairy", "curd"],
            "egg": ["ovum", "roe"],
            "milk": ["dairy milk", "lactose"],
            "yogurt": ["yoghurt", "cultured milk"],
            "butter": ["margarine", "spread"],
            "jam": ["jelly", "preserve"],
            "honey": ["nectar", "syrup"],
            "sugar": ["sweetener", "sucrose"],
            "salt": ["sodium chloride", "seasoning"],
            "pepper": ["spice", "peppercorn"],
            "mustard": ["condiment", "sauce"],
            "ketchup": ["catsup", "tomato sauce"],
            "mayonnaise": ["mayo", "aioli"],
            "oil": ["cooking oil", "lubricant"],
            "vinegar": ["acetic acid", "sour wine"],
            "flour": ["meal", "powder"],
            "rice": ["grain", "paddy"],
            "pasta": ["noodle", "macaroni"],
            "meat": ["flesh", "protein"],
            "chicken": ["poultry", "hen"],
            "beef": ["steak", "veal"],
            "pork": ["ham", "bacon"],
            "fish": ["seafood", "finfish"],
            "shrimp": ["prawn", "crustacean"],
            "crab": ["crustacean", "shellfish"],
            "lobster": ["crayfish", "crustacean"],
            "oyster": ["clam", "mussel"],
            "apple": ["fruit", "gala apple"],
            "banana": ["fruit", "plantain"],
            "orange": ["citrus", "fruit"],
            "lemon": ["citrus", "lime"],
            "grape": ["berry", "vine fruit"],
            "strawberry": ["berry", "garden strawberry"],
            "blueberry": ["berry", "huckleberry"],
            "raspberry": ["berry", "cane fruit"],
            "pineapple": ["ananas", "tropical fruit"],
            "mango": ["tropical fruit"],
            "avocado": ["alligator pear"],
            "tomato": ["fruit", "vegetable"],
            "potato": ["spud", "tuber"],
            "onion": ["bulb", "allium"],
            "garlic": ["clove", "allium"],
            "carrot": ["root vegetable"],
            "broccoli": ["calabrese", "green vegetable"],
            "cabbage": ["colewort", "brassica"],
            "lettuce": ["salad greens", "romaine"],
            "spinach": ["leafy green"],
            "cucumber": ["gourd", "vegetable"],
            "bell pepper": ["capsicum", "sweet pepper"],
            "chili pepper": ["chilli", "hot pepper"],
            "mushroom": ["fungus", "toadstool"],
            "corn": ["maize", "sweet corn"],
            "bean": ["legume", "pod"],
            "pea": ["legume", "pod"],
            "nut": ["seed", "kernel"],
            "peanut": ["groundnut", "goober"],
            "almond": ["nut", "drupe"],
            "walnut": ["nut", "juglans"],
            "pecan": ["nut", "hickory"],
            "cashew": ["nut", "anacardium"],
            "pistachio": ["nut", "green almond"],
            "sunflower seed": ["seed", "achenes"],
            "pumpkin seed": ["pepita", "seed"],
            "sesame seed": ["seed", "benne seed"],
            "flower": ["blossom", "bloom"],
            "rose": ["flower", "rosa"],
            "tulip": ["flower", "tulipa"],
            "daisy": ["flower", "bellis"],
            "lily": ["flower", "lilium"],
            "sunflower": ["flower", "helianthus"],
            "tree": ["plant", "foliage"],
            "bush": ["shrub", "thicket"],
            "grass": ["lawn", "turf"],
            "leaf": ["foliage", "blade"],
            "rock": ["stone", "boulder"],
            "mountain": ["hill", "peak"],
            "river": ["stream", "creek"],
            "lake": ["pond", "loch"],
            "ocean": ["sea", "deep"],
            "beach": ["shore", "coast"],
            "desert": ["wasteland", "arid land"],
            "forest": ["woods", "jungle"],
            "garden": ["park", "yard"],
            "farm": ["ranch", "plantation"],
            "city": ["town", "metropolis"],
            "village": ["hamlet", "settlement"],
            "road": ["street", "avenue"],
            "bridge": ["overpass", "viaduct"],
            "tunnel": ["underpass", "subway"],
            "building": ["structure", "edifice"],
            "house": ["home", "dwelling"],
            "apartment": ["flat", "condo"],
            "skyscraper": ["tower", "high-rise"],
            "church": ["cathedral", "chapel"],
            "mosque": ["masjid", "place of worship"],
            "temple": ["shrine", "pagoda"],
            "castle": ["fortress", "palace"],
            "tower": ["spire", "minaret"],
            "monument": ["memorial", "statue"],
            "fountain": ["water feature", "jet"],
            "statue": ["sculpture", "figure"],
            "bench": ["seat", "pew"],
            "table": ["desk", "counter"],
            "chair": ["seat", "stool"],
            "bed": ["cot", "bunk"],
            "sofa": ["couch", "settee"],
            "cabinet": ["cupboard", "locker"],
            "shelf": ["rack", "ledge"],
            "drawer": ["compartment", "pull-out"],
            "mirror": ["looking glass", "reflector"],
            "clock": ["timepiece", "watch"],
            "painting": ["artwork", "picture"],
            "photo": ["picture", "snapshot"],
            "television": ["tv", "monitor"],
            "computer": ["laptop", "desktop"],
            "phone": ["smartphone", "mobile"],
            "keyboard": ["keypad", "piano"],
            "mouse": ["computer mouse", "rodent"],
            "speaker": ["loudspeaker", "monitor"],
            "headphone": ["earphone", "headset"],
            "microphone": ["mic", "mike"],
            "camera": ["camcorder", "webcam"],
            "printer": ["scanner", "copier"],
            "router": ["modem", "gateway"],
            "fan": ["ventilator", "blower"],
            "heater": ["radiator", "furnace"],
            "air conditioner": ["ac", "cooler"],
            "refrigerator": ["fridge", "icebox"],
            "microwave": ["micro"],
            "oven": ["stove", "range"],
            "dishwasher": ["dish washer"],
            "washing machine": ["washer", "laundry machine"],
            "dryer": ["tumble dryer"],
            "vacuum cleaner": ["hoover", "vacuum"],
            "broom": ["brush", "sweeper"],
            "mop": ["swab"],
            "bucket": ["pail", "can"],
            "sponge": ["loofah", "scourer"],
            "soap": ["detergent", "cleanser"],
            "shampoo": ["conditioner"],
            "toothbrush": ["tooth brush"],
            "toothpaste": ["tooth paste"],
            "towel": ["cloth", "napkin"],
            "mirror": ["looking glass", "reflector"],
            "comb": ["brush"],
            "hair dryer": ["blow dryer"],
            "razor": ["shaver"],
            "scissors": ["shears"],
            "needle": ["pin", "stylus"],
            "thread": ["yarn", "filament"],
            "button": ["fastener", "switch"],
            "zipper": ["slide fastener"],
            "key": ["fob", "opener"],
            "lock": ["fastening", "clasp"],
            "chain": ["link", "shackle"],
            "rope": ["cord", "cable"],
            "ladder": ["steps", "stairway"],
            "tool box": ["toolbox"],
            "hammer": ["mallet", "gavel"],
            "saw": ["hacksaw", "handsaw"],
            "drill": ["borer", "press"],
            "tape measure": ["measuring tape"],
            "ruler": ["straightedge"],
            "pencil": ["graphite pencil"],
            "pen": ["ballpoint pen", "fountain pen"],
            "paper": ["sheet", "document"],
            "book": ["volume", "tome"],
            "newspaper": ["paper", "journal"],
            "magazine": ["periodical", "glossy"],
            "envelope": ["mailer", "wrapper"],
            "stamp": ["postage stamp"],
            "card": ["greeting card", "playing card"],
            "gift": ["present", "offering"],
            "balloon": ["air balloon", "blimp"],
            "candle": ["taper", "wick"],
            "cake": ["pastry", "gateau"],
            "cookie": ["biscuit", "cracker"],
            "candy": ["sweet", "confectionery"],
            "chocolate": ["cocoa", "bonbon"],
            "ice cream": ["gelato", "sorbet"],
            "juice": ["nectar", "drink"],
            "soda": ["pop", "fizzy drink"],
            "beer": ["ale", "lager"],
            "wine": ["vino", "grape wine"],
            "cocktail": ["mixed drink", "aperitif"],
            "pizza": ["pie", "slice"],
            "burger": ["hamburger", "cheeseburger"],
            "fries": ["chips", "french fries"],
            "salad": ["greens", "coleslaw"],
            "soup": ["broth", "stew"],
            "bread": ["loaf", "bun"],
            "cheese": ["dairy", "curd"],
            "egg": ["ovum", "roe"],
            "milk": ["dairy milk", "lactose"],
            "yogurt": ["yoghurt", "cultured milk"],
            "butter": ["margarine", "spread"],
            "jam": ["jelly", "preserve"],
            "honey": ["nectar", "syrup"],
            "sugar": ["sweetener", "sucrose"],
            "salt": ["sodium chloride", "seasoning"],
            "pepper": ["spice", "peppercorn"],
            "mustard": ["condiment", "sauce"],
            "ketchup": ["catsup", "tomato sauce"],
            "mayonnaise": ["mayo", "aioli"],
            "oil": ["cooking oil", "lubricant"],
            "vinegar": ["acetic acid", "sour wine"],
            "flour": ["meal", "powder"],
            "rice": ["grain", "paddy"],
            "pasta": ["noodle", "macaroni"],
            "meat": ["flesh", "protein"],
            "chicken": ["poultry", "hen"],
            "beef": ["steak", "veal"],
            "pork": ["ham", "bacon"],
            "fish": ["seafood", "finfish"],
            "shrimp": ["prawn", "crustacean"],
            "crab": ["crustacean", "shellfish"],
            "lobster": ["crayfish", "crustacean"],
            "oyster": ["clam", "mussel"],
            "apple": ["fruit", "gala apple"],
            "banana": ["fruit", "plantain"],
            "orange": ["citrus", "fruit"],
            "lemon": ["citrus", "lime"],
            "grape": ["berry", "vine fruit"],
            "strawberry": ["berry", "garden strawberry"],
            "blueberry": ["berry", "huckleberry"],
            "raspberry": ["berry", "cane fruit"],
            "pineapple": ["ananas", "tropical fruit"],
            "mango": ["tropical fruit"],
            "avocado": ["alligator pear"],
            "tomato": ["fruit", "vegetable"],
            "potato": ["spud", "tuber"],
            "onion": ["bulb", "allium"],
            "garlic": ["clove", "allium"],
            "carrot": ["root vegetable"],
            "broccoli": ["calabrese", "green vegetable"],
            "cabbage": ["colewort", "brassica"],
            "lettuce": ["salad greens", "romaine"],
            "spinach": ["leafy green"],
            "cucumber": ["gourd", "vegetable"],
            "bell pepper": ["capsicum", "sweet pepper"],
            "chili pepper": ["chilli", "hot pepper"],
            "mushroom": ["fungus", "toadstool"],
            "corn": ["maize", "sweet corn"],
            "bean": ["legume", "pod"],
            "pea": ["legume", "pod"],
            "nut": ["seed", "kernel"],
            "peanut": ["groundnut", "goober"],
            "almond": ["nut", "drupe"],
            "walnut": ["nut", "juglans"],
            "pecan": ["nut", "hickory"],
            "cashew": ["nut", "anacardium"],
            "pistachio": ["nut", "green almond"],
            "sunflower seed": ["seed", "achenes"],
            "pumpkin seed": ["pepita", "seed"],
            "sesame seed": ["seed", "benne seed"],
        })

    def _normalize_target(self, target: str) -> str:
        target = target.lower().strip()
        # Fix plural stripping: use re.sub(r's$', '', target)
        target = re.sub(r's$', '', target)
        return target

    def _get_prompts(self, target: str) -> List[str]:
        normalized_target = self._normalize_target(target)
        prompts = [
            f"a photo of a {normalized_target}",
            f"a {normalized_target} in this image",
        ]
        # Add aliases
        for alias in self.alias_mapping.get(normalized_target, []):
            prompts.append(f"a photo of a {alias}")
            prompts.append(f"a {alias} in this image")
        return prompts

    async def _get_tile_images(self, iframe: Page) -> List[Image.Image]:
        # This assumes the tiles are presented as background images or <img> tags
        # You might need to adjust selectors based on actual hCaptcha implementation
        tiles_data = await iframe.evaluate(r"""
            () => {
                const tiles = Array.from(document.querySelectorAll('.challenge-image .image-wrapper .image'));
                return tiles.map(tile => {
                    const style = window.getComputedStyle(tile);
                    const bgImage = style.backgroundImage;
                    if (bgImage && bgImage !== 'none') {
                        const urlMatch = bgImage.match(/url\(\"(.*?)\"\)/);
                        if (urlMatch && urlMatch[1]) {
                            return urlMatch[1];
                        }
                    }
                    // Fallback for <img> tags or other structures
                    const img = tile.querySelector('img');
                    if (img && img.src) {
                        return img.src;
                    }
                    return null;
                }).filter(Boolean);
            }
        """)

        images = []
        for data_url in tiles_data:
            if data_url.startswith('data:image/'):
                header, encoded = data_url.split(',', 1)
                img_bytes = base64.b64decode(encoded)
                images.append(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
            elif data_url.startswith('http'):
                # For simplicity, we'll assume direct image URLs are rare or handled by Playwright's context
                # In a real scenario, you'd fetch these URLs asynchronously
                print(f"Warning: Direct image URL found, fetching might be slow or blocked: {data_url}")
                # For now, we'll skip direct URLs or assume they are not the primary source
                pass
        return images

    async def _get_challenge_info(self, iframe: Page) -> Tuple[str, List[Image.Image]]:
        # Get the target text (e.g., 
        # Get the target text (e.g., "Please select all images containing a bus")
        # This selector might need adjustment based on hCaptcha's actual DOM structure
        try:
            challenge_text = await iframe.locator(".challenge-header .text").text_content()
            if not challenge_text:
                challenge_text = await iframe.locator(".challenge-header").text_content()
            
            # Extract the target word from the challenge text
            match = re.search(r'select all images with a\s([a-zA-Z0-9\s]+)', challenge_text, re.IGNORECASE)
            if not match:
                match = re.search(r'select all images of\s([a-zA-Z0-9\s]+)', challenge_text, re.IGNORECASE)
            if not match:
                match = re.search(r'click all images containing a\s([a-zA-Z0-9\s]+)', challenge_text, re.IGNORECASE)
            if not match:
                match = re.search(r'click all images of\s([a-zA-Z0-9\s]+)', challenge_text, re.IGNORECASE)
            
            if match:
                target = match.group(1).strip()
                print(f"Detected hCaptcha target: {target}")
            else:
                print(f"Could not extract target from challenge text: {challenge_text}")
                target = ""
        except Exception as e:
            print(f"Error getting challenge text: {e}")
            target = ""

        # Get tile images
        tiles = await iframe.locator(".challenge-image .image-wrapper .image").all()
        images = []
        for tile in tiles:
            # Attempt to get background image first
            bg_image_url = await tile.evaluate("element => window.getComputedStyle(element).backgroundImage")
            if bg_image_url and bg_image_url != 'none':
                match = re.search(r'url\(\"(.*?)\"\)', bg_image_url)
                if match:
                    image_url = match.group(1)
                    if image_url.startswith("data:image/"):
                        header, encoded = image_url.split(",", 1)
                        img_bytes = base64.b64decode(encoded)
                        images.append(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
                    else:
                        # For external URLs, we'd need to fetch them. For now, we'll skip or use a placeholder.
                        # In a real scenario, you'd use Playwright to fetch the image or handle it differently.
                        print(f"Warning: External image URL found, skipping for now: {image_url}")
                        images.append(Image.new("RGB", (100, 100), color = 'red')) # Placeholder
            else:
                # Fallback for <img> tags inside the tile div
                img_element = tile.locator("img")
                if await img_element.count() > 0:
                    img_src = await img_element.get_attribute("src")
                    if img_src and img_src.startswith("data:image/"):
                        header, encoded = img_src.split(",", 1)
                        img_bytes = base64.b64decode(encoded)
                        images.append(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
                    else:
                        print(f"Warning: img tag with external src or no src found, skipping for now: {img_src}")
                        images.append(Image.new("RGB", (100, 100), color = 'blue')) # Placeholder
                else:
                    print("Warning: No image found for tile, using placeholder.")
                    images.append(Image.new("RGB", (100, 100), color = 'green')) # Placeholder

        return target, images

    async def _solve_challenge(self, iframe: Page, target: str, images: List[Image.Image]) -> List[int]:
        clip_model = await self._get_clip_model()

        # Prepare prompts
        positive_prompts = self._get_prompts(target)
        all_prompts = positive_prompts + self.negative_prompts

        # Encode text features
        text_features = await clip_model.get_text_features(all_prompts)
        positive_text_features = text_features[:len(positive_prompts)]
        negative_text_features = text_features[len(positive_prompts):]

        # Encode image features (batch inference)
        image_features = await clip_model.get_image_features(images)

        # Compute similarity scores
        positive_similarities = (image_features @ positive_text_features.T).mean(dim=1)
        negative_similarities = (image_features @ negative_text_features.T).max(dim=1).values

        # Contrast scoring
        scores = positive_similarities - negative_similarities

        # Adaptive thresholding
        selected_tiles = []
        if len(scores) == 0:
            return []

        max_score = scores.max().item()
        min_score = scores.min().item()
        score_range = max_score - min_score

        if score_range > 0.15: # Bimodal gap
            # Use midpoint between highest negative and lowest positive as threshold
            # This is a simplification, a true bimodal distribution would need more analysis
            # For now, we'll use the config threshold if a clear gap isn't obvious from min/max
            threshold = self.config.clip_confidence_threshold
            if max_score > self.config.clip_confidence_threshold and min_score < self.config.clip_confidence_threshold:
                # If scores span the threshold, try to find a natural separation
                # This is a heuristic, can be improved with clustering or more sophisticated analysis
                pass # Stick to default threshold for now
            selected_tiles = [i for i, score in enumerate(scores) if score > threshold]
        elif max_score < 0.4: # All low scores
            # Pick top 3 highest scores
            top_3_indices = torch.topk(scores, min(3, len(scores))).indices.tolist()
            selected_tiles = top_3_indices
        elif min_score > 0.5: # All high scores
            # Pick above median
            median_score = torch.median(scores).item()
            selected_tiles = [i for i, score in enumerate(scores) if score > median_score]
        else: # Default to confidence threshold
            selected_tiles = [i for i, score in enumerate(scores) if score > self.config.clip_confidence_threshold]

        # Ensure 1-6 tiles are selected
        if not (1 <= len(selected_tiles) <= 6):
            # Fallback: if too many or too few, pick top N based on score magnitude
            # This is a simple heuristic, could be improved.
            sorted_indices = torch.argsort(scores, descending=True).tolist()
            selected_tiles = sorted_indices[:min(6, max(1, len(sorted_indices)))]

        return selected_tiles

    async def _click_tiles(self, iframe: Page, tile_indices: List[int]):
        tiles = await iframe.locator(".challenge-image .image-wrapper .image").all()
        for i in tile_indices:
            if i < len(tiles):
                tile = tiles[i]
                box = await tile.bounding_box()
                if box:
                    x = box["x"] + box["width"] / 2
                    y = box["y"] + box["height"] / 2

                    # Bezier curve mouse movements
                    start_x, start_y = await self.playwright_solver.context.pages[0].mouse.position()
                    if start_x is None or start_y is None:
                        start_x, start_y = x, y # Fallback if mouse position not available

                    control_x = random.uniform(min(start_x, x), max(start_x, x))
                    control_y = random.uniform(min(start_y, y), max(start_y, y))

                    points = [
                        (start_x, start_y),
                        (control_x, control_y),
                        (x, y)
                    ]
                    
                    num_steps = random.randint(10, 20)
                    for i in range(num_steps):
                        t = i / (num_steps - 1)
                        # Quadratic Bezier curve calculation
                        bx = (1-t)**2 * points[0][0] + 2*(1-t)*t * points[1][0] + t**2 * points[2][0]
                        by = (1-t)**2 * points[0][1] + 2*(1-t)*t * points[1][1] + t**2 * points[2][1]
                        await self.playwright_solver.context.pages[0].mouse.move(bx, by, steps=1)
                        await asyncio.sleep(random.uniform(0.01, 0.05))

                    await self.playwright_solver.context.pages[0].mouse.click(x, y)
                    await self._apply_rate_limit()

    async def solve(self, page: Page) -> bool:
        start_time = time.time()
        detector = ChallengeDetector(page)

        # Ensure browser is launched and page is ready
        if self.playwright_solver.browser is None:
            await self.playwright_solver._launch_browser()
        
        # Navigate to the page if not already there (assuming page is already passed in)
        # await page.goto(url) # This might not be needed if the page is already at the challenge

        for round_num in range(self.config.max_challenge_rounds):
            print(f"Attempting hCaptcha solve round {round_num + 1}/{self.config.max_challenge_rounds}")
            round_start_time = time.time()

            # Wait for hCaptcha iframe to appear and be ready
            try:
                await page.wait_for_selector('iframe[src*="hcaptcha.com/captcha"]', timeout=self.config.timeout * 1000)
                iframe_locator = page.frame_locator('iframe[src*="hcaptcha.com/captcha"]')
                iframe = await iframe_locator.frame()
                if not iframe:
                    print("hCaptcha iframe not found or not loaded.")
                    continue
                
                # Wait for challenge elements inside the iframe
                await iframe.wait_for_selector(".challenge-image", timeout=self.config.timeout * 1000)
                await iframe.wait_for_selector(".challenge-header", timeout=self.config.timeout * 1000)

            except Exception as e:
                print(f"hCaptcha challenge elements not found within timeout: {e}")
                if await detector.is_solved():
                    print("Captcha already solved, exiting.")
                    return True
                return False # Cannot proceed without challenge elements

            target, images = await self._get_challenge_info(iframe)
            if not target or not images:
                print("Failed to get challenge info (target or images). Retrying round.")
                continue

            selected_tiles = await self._solve_challenge(iframe, target, images)
            print(f"Selected tiles: {selected_tiles}")

            if selected_tiles:
                await self._click_tiles(iframe, selected_tiles)
                # Click the verify button
                try:
                    verify_button = iframe.locator(".verify-button")
                    if await verify_button.is_visible():
                        await verify_button.click()
                        await self._apply_rate_limit()
                        print("Clicked verify button.")
                except Exception as e:
                    print(f"Error clicking verify button: {e}")

            # Wait for min_solve_time_per_round before checking if solved
            time_elapsed_this_round = time.time() - round_start_time
            if time_elapsed_this_round < self.config.min_solve_time_per_round:
                await asyncio.sleep(self.config.min_solve_time_per_round - time_elapsed_this_round)

            # Check if solved
            if await detector.is_solved():
                print("hCaptcha solved successfully!")
                return True
            else:
                print("hCaptcha not yet solved, attempting next round if available.")
                # If not solved, hCaptcha usually reloads with new images. No explicit refresh needed.

        print("Failed to solve hCaptcha after maximum rounds.")
        return False

    async def close(self):
        await self.playwright_solver.close()

# --- Selenium Solver (Placeholder for future expansion) --- #
class SeleniumSolver(CaptchaSolver):
    async def solve(self, page) -> bool:
        print("SeleniumSolver not implemented.")
        return False

# --- CLI Entry Point (Example Usage) --- #
async def main():
    config = SolverConfig(
        headless=False,
        clip_confidence_threshold=0.55,
        max_challenge_rounds=3,
        timeout=30,
        min_solve_time_per_round=2.5,
        rate_limit_min_delay=0.1,
        rate_limit_max_delay=0.35
    )
    solver = GodSolver(config)
    try:
        # Example usage: navigate to a page with hCaptcha
        # For a real test, you'd need a URL that reliably presents an hCaptcha
        # For now, this is just a placeholder to show how to use it.
        # page = await solver.playwright_solver.new_page()
        # await page.goto("https://www.hcaptcha.com/" ) # Example, might not trigger challenge
        # await solver.solve(page)
        print("GodSolver initialized. To use, pass a Playwright page object to solver.solve(page).")
    finally:
        await solver.close()

if __name__ == "__main__":

    asyncio.run(main())

