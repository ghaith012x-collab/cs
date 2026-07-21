import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

from server import DiscordAutomation, run_discord_automation


class AppHost:
    def __init__(self):
        self._automation: Optional[DiscordAutomation] = None
        self._running = False
        self._config_path = "config.json"

    def load_config(self, path: str = "config.json") -> dict:
        default_config = {
            "email": "test@example.com",
            "username": "user_{random}",
            "password": "Password123!",
            "headless": False,
            "camera_interval": 3
        }
        
        if os.path.exists(path):
            with open(path, 'r') as f:
                config = json.load(f)
                for key, value in default_config.items():
                    if key not in config:
                        config[key] = value
                return config
        
        return default_config

    def save_config(self, config: dict, path: str = "config.json") -> None:
        with open(path, 'w') as f:
            json.dump(config, f, indent=2)

    def show_banner(self) -> None:
        print("=" * 50)
        print("  Discord Automation Suite")
        print("  Version 1.0.0")
        print("=" * 50)
        print()

    def show_help(self) -> None:
        print("Commands:")
        print("  start       - Start Discord automation")
        print("  stop        - Stop automation")
        print("  status      - Check status")
        print("  config      - Show current config")
        print("  screenshot  - Get latest screenshot")
        print("  help        - Show this help")
        print("  exit        - Exit the application")
        print()

    def display_screenshots(self, count: int = 5) -> None:
        if not self._automation:
            print("Automation not initialized")
            return
        
        screenshots = self._automation.get_screenshots()
        print(f"\nAvailable screenshots: {len(screenshots)}")
        for i, _ in enumerate(screenshots[-count:]):
            print(f"  [{len(screenshots) - count + i + 1}] Screenshot captured")

    async def start_automation(self) -> None:
        if self._automation and self._running:
            print("Automation already running")
            return
        
        config = self.load_config(self._config_path)
        
        self._automation = DiscordAutomation(headless=config.get('headless', False))
        self._running = True
        
        try:
            await self._automation.initialize()
            self._automation.load_config(self._config_path)
            
            success = await self._automation.start_discord_signup()
            
            if success:
                print("✓ Automation completed successfully")
            else:
                print("✗ Automation failed")
            
            await self._capture_periodic_screenshots(config.get('camera_interval', 3))
            
        except Exception as e:
            print(f"Error during automation: {e}")
        finally:
            await self._cleanup()

    async def _capture_periodic_screenshots(self, interval: int) -> None:
        print(f"Capturing screenshots every {interval} seconds...")
        while self._running:
            try:
                await self._automation.capture_screenshot()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Screenshot error: {e}")
                await asyncio.sleep(interval)

    async def stop_automation(self) -> None:
        self._running = False
        if self._automation:
            await self._automation.close()
            self._automation = None
        print("Automation stopped")

    async def _cleanup(self) -> None:
        self._running = False
        if self._automation:
            await self._automation.close()
            self._automation = None

    async def run_shell(self) -> None:
        self.show_banner()
        self.show_help()
        
        while True:
            try:
                cmd = input("\n> ").strip().lower()
                
                if cmd in ['exit', 'quit', 'q']:
                    if self._running:
                        await self.stop_automation()
                    print("Goodbye!")
                    break
                
                elif cmd == 'start':
                    await self.start_automation()
                
                elif cmd == 'stop':
                    await self.stop_automation()
                
                elif cmd == 'status':
                    if self._automation and self._running:
                        print("Status: Running")
                        screenshots = len(self._automation.get_screenshots())
                        print(f"Screenshots captured: {screenshots}")
                    else:
                        print("Status: Stopped")
                
                elif cmd == 'config':
                    config = self.load_config(self._config_path)
                    print(json.dumps(config, indent=2))
                
                elif cmd == 'screenshot':
                    if self._automation:
                        self.display_screenshots()
                    else:
                        print("Automation not running")
                
                elif cmd == 'help':
                    self.show_help()
                
                else:
                    print(f"Unknown command: {cmd}")
                    self.show_help()
            
            except KeyboardInterrupt:
                if self._running:
                    await self.stop_automation()
                print("\nGoodbye!")
                break
            
            except Exception as e:
                print(f"Error: {e}")


def create_sample_config() -> None:
    config = {
        "email": "test@example.com",
        "username": "discord_user_1234",
        "password": "SecurePassword123!",
        "headless": False,
        "camera_interval": 3
    }
    
    with open("config.json", 'w') as f:
        json.dump(config, f, indent=2)
    
    print("Created config.json with sample values")


async def main():
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        
        if arg == '--init':
            create_sample_config()
            return
        
        if arg == '--headless':
            app = AppHost()
            config = app.load_config()
            config['headless'] = True
            app.save_config(config)
            await run_discord_automation()
            return
        
        if arg == '--single':
            await run_discord_automation()
            return
    
    app = AppHost()
    await app.run_shell()


if __name__ == "__main__":
    asyncio.run(main())