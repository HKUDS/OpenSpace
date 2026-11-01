import platform
import os
import logging
from typing import Optional, Tuple
from PIL import Image
import pyautogui

logger = logging.getLogger(__name__)

platform_name = platform.system()


class ScreenshotHelper:
    def __init__(self):
        self.platform = platform_name
        self.adapter = None
        
        try:
            if platform_name == "Darwin":
                from ..platform_adapters.macos_adapter import MacOSAdapter
                self.adapter = MacOSAdapter()
            elif platform_name == "Linux":
                from ..platform_adapters.linux_adapter import LinuxAdapter
                self.adapter = LinuxAdapter()
            elif platform_name == "Windows":
                from ..platform_adapters.windows_adapter import WindowsAdapter
                self.adapter = WindowsAdapter()
        except ImportError as e:
            logger.warning(f"Failed to import platform adapter: {e}")
    
    def capture(self, output_path: str, with_cursor: bool = True) -> bool:
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            if with_cursor and self.adapter:
                # Use platform-specific method to capture screenshot (with cursor)
                return self.adapter.capture_screenshot_with_cursor(output_path)
            else:
                # Use pyautogui to capture screenshot (without cursor)
                screenshot = pyautogui.screenshot()
                screenshot.save(output_path)
                logger.info(f"Screenshot successfully (without cursor): {output_path}")
                return True
                
        except Exception as e:
            logger.error(f"Screenshot failed: {e}")
            return False
    
    def capture_region(
        self, 
        output_path: str, 
        x: int, 
        y: int, 
        width: int, 
        height: int
    ) -> bool:
        """
        Capture specified screen region
        
        Args:
            output_path: Output path
            x: Starting x coordinate
            y: Starting y coordinate
            width: Width
            height: Height
            
        Returns:
            Whether successful
        """
        try:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            screenshot = pyautogui.screenshot(region=(x, y, width, height))
            screenshot.save(output_path)
            logger.info(f"Region screenshot successfully: {output_path}")
            return True
            
        except Exception as e:
            logger.error(f"Region screenshot failed: {e}")
            return False
    
    def get_screen_size(self) -> Tuple[int, int]:
        """
        Get screen size
        
        Returns:
            (width, height)
        """
        try:
            size = pyautogui.size()
            return (size.width, size.height)
        except Exception as e:
            logger.error(f"Failed to get screen size: {e}")
            return (1920, 1080)  # Default value
    
    def get_cursor_position(self) -> Tuple[int, int]:
        """
        Get cursor position
        
        Returns:
            (x, y)
        """
        try:
            pos = pyautogui.position()
            return (pos.x, pos.y)
        except Exception as e:
            logger.error(f"Failed to get cursor position: {e}")
            return (0, 0)
    
    def capture_to_base64(self, with_cursor: bool = True) -> Optional[str]:
        """
        Capture screenshot and convert to base64
        
        Args:
            with_cursor: Whether to include cursor
            
        Returns:
            Base64 encoded image string
        """
        import tempfile
        import base64
        
        try:
            # Create temporary file
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                tmp_path = tmp.name
            
            # Capture screenshot
            if self.capture(tmp_path, with_cursor):
                # Read and encode
                with open(tmp_path, 'rb') as f:
                    img_data = f.read()
                    img_base64 = base64.b64encode(img_data).decode('utf-8')
                
                # Delete temporary file
                os.remove(tmp_path)
                
                return img_base64
            else:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                return None
                
        except Exception as e:
            logger.error(f"Failed to convert screenshot to base64: {e}")
            return None
    
    def compare_screenshots(self, path1: str, path2: str) -> float:
        """
        Compare similarity between two screenshots
        
        Args:
            path1: First image path
            path2: Second image path
            
        Returns:
            Similarity (0-1), 1 means identical
        """
        try:
            from PIL import ImageChops
            import math
            import operator
            from functools import reduce
            
            img1 = Image.open(path1)
            img2 = Image.open(path2)
            
            # Ensure same size
            if img1.size != img2.size:
                # Resize to same size
                img2 = img2.resize(img1.size)
            
            # Calculate difference
            diff = ImageChops.difference(img1, img2)
            
            # Calculate statistics
            stat = diff.histogram()
            sum_of_squares = reduce(
                operator.add,
                map(lambda h, i: h * (i ** 2), stat, range(len(stat)))
            )
            
            # Calculate RMS
            rms = math.sqrt(sum_of_squares / float(img1.size[0] * img1.size[1]))
            
            # Normalize to 0-1, RMS max value is approximately 441 (for RGB)
            similarity = 1 - (rms / 441.0)
            
            return max(0, min(1, similarity))
            
        except Exception as e:
            logger.error(f"Failed to compare screenshots: {e}")
            return 0.0
    
    def annotate_screenshot(
        self, 
        input_path: str, 
        output_path: str, 
        annotations: list
    ) -> bool:
        """
        Add annotations to screenshot
        
        Args:
            input_path: Input image path
            output_path: Output image path
            annotations: List of annotations, each annotation is a dict:
                        {'type': 'rectangle'/'text', 'x': int, 'y': int, 
                         'width': int, 'height': int, 'text': str, 'color': tuple}
            
        Returns:
            Whether successful
        """
        try:
            from PIL import ImageDraw, ImageFont
            
            img = Image.open(input_path)
            draw = ImageDraw.Draw(img)
            
            for annotation in annotations:
                ann_type = annotation.get('type', 'rectangle')
                color = annotation.get('color', (255, 0, 0))
                
                if ann_type == 'rectangle':
                    x = annotation.get('x', 0)
                    y = annotation.get('y', 0)
                    width = annotation.get('width', 100)
                    height = annotation.get('height', 100)
                    
                    draw.rectangle(
                        [(x, y), (x + width, y + height)],
                        outline=color,
                        width=2
                    )
                    
                elif ann_type == 'text':
                    x = annotation.get('x', 0)
                    y = annotation.get('y', 0)
                    text = annotation.get('text', '')
                    
                    try:
                        font = ImageFont.truetype("Arial.ttf", 20)
                    except:
                        font = ImageFont.load_default()
                    
                    draw.text((x, y), text, fill=color, font=font)
            
            img.save(output_path)
            logger.info(f"Annotated screenshot successfully: {output_path}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to annotate screenshot: {e}")
            return False