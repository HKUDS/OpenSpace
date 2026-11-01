import os
import ctypes
import subprocess
from typing import Dict, Any, Optional, List
from openspace.utils.logging import Logger
from PIL import Image, ImageGrab

try:
    from pywinauto import Desktop
    import win32ui
    import win32gui
    import win32con
    import pygetwindow as gw
    WINDOWS_LIBS_AVAILABLE = True
except ImportError:
    WINDOWS_LIBS_AVAILABLE = False

logger = Logger.get_logger(__name__)


class WindowsAdapter:
    """Windows platform-specific functionality adapter"""
    
    def __init__(self):
        if not WINDOWS_LIBS_AVAILABLE:
            logger.warning("Windows libraries are not fully installed, some features may not be available")
        self.available = WINDOWS_LIBS_AVAILABLE
    
    def capture_screenshot_with_cursor(self, output_path: str) -> bool:
        """
        Capture screenshot using ImageGrab (including cursor)
        
        Args:
            output_path: Output file path
            
        Returns:
            Whether successful
        """
        try:
            # Use ImageGrab to capture screenshot
            img = ImageGrab.grab(bbox=None, include_layered_windows=True)
            
            # Try to add cursor
            try:
                if WINDOWS_LIBS_AVAILABLE:
                    cursor, hotspot = self._get_cursor()
                    if cursor:
                        # Get scaling ratio
                        ratio = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100
                        pos_win = win32gui.GetCursorPos()
                        pos = (
                            round(pos_win[0] * ratio - hotspot[0]),
                            round(pos_win[1] * ratio - hotspot[1])
                        )
                        img.paste(cursor, pos, cursor)
                        logger.info("Windows screenshot successfully (with cursor)")
                    else:
                        logger.info("Windows screenshot successfully (without cursor)")
            except Exception as e:
                logger.warning(f"Cannot add cursor to screenshot: {e}")
                logger.info("Windows screenshot successfully (without cursor)")
            
            img.save(output_path)
            return True
            
        except Exception as e:
            logger.error(f"Windows screenshot failed: {e}")
            return False
    
    def _get_cursor(self) -> tuple:
        """
        Get current cursor image and hotspot
        
        Returns:
            (cursor_image, (hotspot_x, hotspot_y))
        """
        try:
            hcursor = win32gui.GetCursorInfo()[1]
            hdc = win32ui.CreateDCFromHandle(win32gui.GetDC(0))
            hbmp = win32ui.CreateBitmap()
            hbmp.CreateCompatibleBitmap(hdc, 36, 36)
            hdc_compatible = hdc.CreateCompatibleDC()
            hdc_compatible.SelectObject(hbmp)
            hdc_compatible.DrawIcon((0, 0), hcursor)
            
            bmpinfo = hbmp.GetInfo()
            bmpstr = hbmp.GetBitmapBits(True)
            cursor = Image.frombuffer(
                'RGB',
                (bmpinfo['bmWidth'], bmpinfo['bmHeight']),
                bmpstr, 'raw', 'BGRX', 0, 1
            ).convert("RGBA")
            
            win32gui.DestroyIcon(hcursor)
            win32gui.DeleteObject(hbmp.GetHandle())
            hdc_compatible.DeleteDC()
            
            # Make black pixels transparent
            pixdata = cursor.load()
            width, height = cursor.size
            for y in range(height):
                for x in range(width):
                    if pixdata[x, y] == (0, 0, 0, 255):
                        pixdata[x, y] = (0, 0, 0, 0)
            
            hotspot = win32gui.GetIconInfo(hcursor)[1:3]
            
            return (cursor, hotspot)
            
        except Exception as e:
            logger.debug(f"Failed to get cursor image: {e}")
            return (None, (0, 0))
    
    def activate_window(self, window_name: str, strict: bool = False) -> Dict[str, Any]:
        """
        Activate window (Windows uses pygetwindow)
        
        Args:
            window_name: Window title
            strict: Whether to strictly match
            
        Returns:
            Result dictionary
        """
        if not WINDOWS_LIBS_AVAILABLE:
            return {'status': 'error', 'message': 'Windows libraries not available'}
        
        try:
            windows = gw.getWindowsWithTitle(window_name)
            
            if not windows:
                logger.warning(f"Window not found: {window_name}")
                return {'status': 'error', 'message': f'Window {window_name} not found'}
            
            window = None
            if strict:
                # Strict match
                for wnd in windows:
                    if wnd.title == window_name:
                        window = wnd
                        break
                if not window:
                    return {'status': 'error', 'message': f'Window {window_name} not found (strict mode)'}
            else:
                window = windows[0]
            
            window.activate()
            logger.info(f"Windows window activated successfully: {window_name}")
            return {'status': 'success', 'message': 'Window activated'}
            
        except Exception as e:
            logger.error(f"Windows window activation failed: {e}")
            return {'status': 'error', 'message': str(e)}
    
    def close_window(self, window_name: str, strict: bool = False) -> Dict[str, Any]:
        """
        Close window (Windows uses pygetwindow)
        
        Args:
            window_name: Window title
            strict: Whether to strictly match
            
        Returns:
            Result dictionary
        """
        if not WINDOWS_LIBS_AVAILABLE:
            return {'status': 'error', 'message': 'Windows libraries not available'}
        
        try:
            windows = gw.getWindowsWithTitle(window_name)
            
            if not windows:
                logger.warning(f"Window not found: {window_name}")
                return {'status': 'error', 'message': f'Window {window_name} not found'}
            
            window = None
            if strict:
                for wnd in windows:
                    if wnd.title == window_name:
                        window = wnd
                        break
                if not window:
                    return {'status': 'error', 'message': f'Window {window_name} not found (strict mode)'}
            else:
                window = windows[0]
            
            window.close()
            logger.info(f"Windows window closed successfully: {window_name}")
            return {'status': 'success', 'message': 'Window closed'}
            
        except Exception as e:
            logger.error(f"Windows window close failed: {e}")
            return {'status': 'error', 'message': str(e)}
    
    def get_accessibility_tree(self, max_depth: int = 10, max_width: int = 50) -> Dict[str, Any]:
        """
        Get Windows accessibility tree (using pywinauto)
        
        Args:
            max_depth: Maximum depth
            max_width: Maximum number of child elements per level
            
        Returns:
            Accessibility tree data
        """
        if not WINDOWS_LIBS_AVAILABLE:
            return {'error': 'Windows accessibility libraries not available'}
        
        try:
            # Get desktop
            desktop = Desktop(backend="uia")
            
            # Serialize accessibility tree
            tree = self._serialize_uia_element(
                desktop, 
                depth=0, 
                max_depth=max_depth,
                max_width=max_width,
                visited=set()
            )
            
            return {
                'tree': tree,
                'platform': 'Windows'
            }
            
        except Exception as e:
            logger.error(f"Windows get accessibility tree failed: {e}")
            return {'error': str(e)}
    
    def _serialize_uia_element(
        self, 
        element, 
        depth: int = 0, 
        max_depth: int = 10,
        max_width: int = 50,
        visited: set = None
    ) -> Optional[Dict[str, Any]]:
        """
        Serialize Windows UIA element to dictionary
        
        Args:
            element: UIA element
            depth: Current depth
            max_depth: Maximum depth
            max_width: Maximum width
            visited: Set of visited elements
            
        Returns:
            Serialized dictionary
        """
        if visited is None:
            visited = set()
        
        if depth > max_depth or element in visited:
            return None
        
        visited.add(element)
        
        try:
            result = {
                'depth': depth
            }
            
            # Get basic attributes
            try:
                result['class_name'] = element.class_name()
            except:
                result['class_name'] = 'unknown'
            
            try:
                result['name'] = element.window_text()
            except:
                result['name'] = ''
            
            # Get states
            states = {}
            state_methods = [
                'is_enabled', 'is_visible', 'is_minimized', 'is_maximized',
                'is_focused', 'is_checked', 'is_selected'
            ]
            
            for method_name in state_methods:
                if hasattr(element, method_name):
                    try:
                        method = getattr(element, method_name)
                        states[method_name] = method()
                    except:
                        pass
            
            if states:
                result['states'] = states
            
            # Get position and size
            try:
                rectangle = element.rectangle()
                result['position'] = {
                    'left': rectangle.left,
                    'top': rectangle.top
                }
                result['size'] = {
                    'width': rectangle.width(),
                    'height': rectangle.height()
                }
            except:
                pass
            
            # Recursively get child elements
            result['children'] = []
            try:
                children = element.children()
                for i, child in enumerate(children[:max_width]):
                    try:
                        child_data = self._serialize_uia_element(
                            child, 
                            depth + 1, 
                            max_depth,
                            max_width,
                            visited
                        )
                        if child_data:
                            result['children'].append(child_data)
                    except Exception as e:
                        logger.debug(f"Cannot serialize child element {i}: {e}")
                        continue
            except Exception as e:
                logger.debug(f"Cannot get child elements: {e}")
            
            return result
            
        except Exception as e:
            logger.debug(f"Failed to serialize element (depth={depth}): {e}")
            return None
    
    def list_windows(self) -> List[Dict[str, Any]]:
        """
        List all windows
        
        Returns:
            Window list
        """
        if not WINDOWS_LIBS_AVAILABLE:
            return []
        
        try:
            windows = gw.getAllWindows()
            
            return [
                {
                    'title': win.title,
                    'left': win.left,
                    'top': win.top,
                    'width': win.width,
                    'height': win.height,
                    'visible': win.visible,
                    'active': win.isActive
                }
                for win in windows
                if win.title  # Only return windows with titles
            ]
            
        except Exception as e:
            logger.error(f"List windows failed: {e}")
            return []
    
    def set_wallpaper(self, image_path: str) -> Dict[str, Any]:
        """
        Set desktop wallpaper
        
        Args:
            image_path: Image path
            
        Returns:
            Result dictionary
        """
        try:
            image_path = os.path.expanduser(image_path)
            image_path = os.path.abspath(image_path)
            
            if not os.path.exists(image_path):
                return {'status': 'error', 'message': f'Image not found: {image_path}'}
            
            # Use Windows API to set wallpaper
            SPI_SETDESKWALLPAPER = 20
            ctypes.windll.user32.SystemParametersInfoW(
                SPI_SETDESKWALLPAPER,
                0,
                image_path,
                3  # SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
            )
            
            logger.info(f"Windows wallpaper set successfully: {image_path}")
            return {'status': 'success', 'message': 'Wallpaper set successfully'}
            
        except Exception as e:
            logger.error(f"Windows set wallpaper failed: {e}")
            return {'status': 'error', 'message': str(e)}
    
    def get_system_info(self) -> Dict[str, Any]:
        """
        Get Windows system information
        
        Returns:
            System information dictionary
        """
        try:
            import platform as plat
            
            return {
                'platform': 'Windows',
                'version': plat.version(),
                'release': plat.release(),
                'edition': plat.win32_edition() if hasattr(plat, 'win32_edition') else 'Unknown',
                'available': self.available
            }
            
        except Exception as e:
            logger.error(f"Failed to get system information: {e}")
            return {
                'platform': 'Windows',
                'error': str(e)
            }
    
    def start_recording(self, output_path: str) -> Dict[str, Any]:
        try:
            try:
                result = subprocess.run(['ffmpeg', '-version'], 
                                      capture_output=True, 
                                      check=True,
                                      timeout=5,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            except (subprocess.CalledProcessError, FileNotFoundError):
                return {
                    'status': 'error',
                    'message': 'ffmpeg not installed. Download from: https://ffmpeg.org/download.html'
                }
            try:
                user32 = ctypes.windll.user32
                width = user32.GetSystemMetrics(0)  # SM_CXSCREEN
                height = user32.GetSystemMetrics(1)  # SM_CYSCREEN
            except:
                width, height = 1920, 1080
            
            command = [
                'ffmpeg',
                '-y',  
                '-f', 'gdigrab',  
                '-draw_mouse', '1',  
                '-framerate', '30',
                '-video_size', f'{width}x{height}',
                '-i', 'desktop',  
                '-c:v', 'libx264',
                '-preset', 'ultrafast', 
                '-pix_fmt', 'yuv420p', 
                '-r', '30', 
                output_path
            ]
            
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            
            import time
            time.sleep(1)
            
            if process.poll() is not None:
                error_output = process.stderr.read() if process.stderr else "Unknown error"
                return {
                    'status': 'error',
                    'message': f'Failed to start recording: {error_output}'
                }
            
            logger.info(f"Windows recording started: {output_path}")
            return {
                'status': 'success',
                'message': 'Recording started',
                'process': process
            }
            
        except Exception as e:
            logger.error(f"Windows start recording failed: {e}")
            return {
                'status': 'error',
                'message': str(e)
            }
    
    def stop_recording(self, process) -> Dict[str, Any]:
        try:
            if not process or process.poll() is not None:
                return {
                    'status': 'error',
                    'message': 'No recording in progress'
                }
            
            import signal
            try:
                process.send_signal(signal.CTRL_C_EVENT)
            except:
                process.terminate()
                
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                logger.warning("ffmpeg did not respond, killing process")
                process.kill()
                process.wait()
            
            logger.info("Windows recording stopped successfully")
            return {
                'status': 'success',
                'message': 'Recording stopped'
            }
            
        except Exception as e:
            logger.error(f"Windows stop recording failed: {e}")
            return {
                'status': 'error',
                'message': str(e)
            }
    
    def get_running_applications(self) -> List[Dict[str, str]]:
        """
        Get list of all running applications
        
        Returns:
            Application list
        """
        if not WINDOWS_LIBS_AVAILABLE:
            return []
        
        try:
            import psutil
            
            apps = []
            seen_names = set()
            
            for proc in psutil.process_iter(['pid', 'name', 'exe']):
                try:
                    pinfo = proc.info
                    name = pinfo['name']
                    exe = pinfo['exe']
                    
                    # Skip system processes
                    if not exe or name in ['System', 'Registry', 'svchost.exe', 'csrss.exe']:
                        continue
                    
                    # Skip duplicates
                    if name in seen_names:
                        continue
                    
                    seen_names.add(name)
                    
                    apps.append({
                        'name': name,
                        'pid': pinfo['pid'],
                        'path': exe or ''
                    })
                    
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
            
            return apps
            
        except ImportError:
            logger.warning("psutil not installed, cannot get running applications")
            return []
        except Exception as e:
            logger.error(f"Failed to get running applications list: {e}")
            return []
    
    def get_screen_size(self) -> Dict[str, int]:
        """
        Get screen size
        
        Returns:
            Screen size dictionary
        """
        try:
            user32 = ctypes.windll.user32
            width = user32.GetSystemMetrics(0)  # SM_CXSCREEN
            height = user32.GetSystemMetrics(1)  # SM_CYSCREEN
            return {'width': width, 'height': height}
        except Exception as e:
            logger.error(f"Failed to get screen size: {e}")
            return {'width': 1920, 'height': 1080}  # Default value
    
    def get_terminal_output(self) -> Optional[str]:
        """
        Get terminal output (Windows Command Prompt, PowerShell, or Windows Terminal)
        
        Note: Due to Windows architecture, getting terminal output is complex.
        This method attempts to find active console windows.
        
        Returns:
            Terminal output content (limited functionality on Windows)
        """
        try:
            # Windows doesn't provide easy access to terminal content like Linux/macOS
            # This is a limitation of the Windows platform
            # We can try to use PowerShell to get recent command history
            
            # Try to get PowerShell history
            try:
                history_path = os.path.expanduser(
                    '~\\AppData\\Roaming\\Microsoft\\Windows\\PowerShell\\PSReadLine\\ConsoleHost_history.txt'
                )
                if os.path.exists(history_path):
                    with open(history_path, 'r', encoding='utf-8', errors='ignore') as f:
                        # Get last 50 lines
                        lines = f.readlines()
                        recent_history = ''.join(lines[-50:])
                        if recent_history:
                            return f"PowerShell History (last 50 commands):\n{recent_history}"
            except Exception as e:
                logger.debug(f"Cannot read PowerShell history: {e}")
            
            # Try to get Command Prompt history using doskey
            try:
                result = subprocess.run(
                    ['doskey', '/history'],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                if result.returncode == 0 and result.stdout:
                    return f"Command Prompt History:\n{result.stdout}"
            except Exception as e:
                logger.debug(f"Cannot get Command Prompt history: {e}")
            
            logger.warning("Windows terminal output is limited - only command history available")
            return None
            
        except Exception as e:
            logger.error(f"Failed to get terminal output: {e}")
            return None

