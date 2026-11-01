import subprocess
import os
from typing import Dict, Any, Optional, List
from openspace.utils.logging import Logger
from PIL import Image
import pyautogui

try:
    import pyatspi
    from pyatspi import Accessible, StateType, STATE_SHOWING
    import Xlib
    from Xlib import display, X
    LINUX_LIBS_AVAILABLE = True
except ImportError:
    LINUX_LIBS_AVAILABLE = False

logger = Logger.get_logger(__name__)


class LinuxAdapter:
    
    def __init__(self):
        if not LINUX_LIBS_AVAILABLE:
            logger.warning("Linux libraries are not fully installed, some features may not be available")
        self.available = LINUX_LIBS_AVAILABLE
    
    def capture_screenshot_with_cursor(self, output_path: str) -> bool:
        """
        Use pyautogui + pyxcursor to capture screenshot (including cursor)
        
        Args:
            output_path: Output file path
            
        Returns:
            Whether the screenshot is successful
        """
        try:
            # Use pyautogui to capture screenshot
            screenshot = pyautogui.screenshot()
            
            # Try to add cursor
            try:
                # Import pyxcursor (should be in the same directory)
                import sys
                import os
                sys.path.insert(0, os.path.dirname(__file__))
                
                from pyxcursor import Xcursor
                
                cursor_obj = Xcursor()
                imgarray = cursor_obj.getCursorImageArrayFast()
                cursor_img = Image.fromarray(imgarray)
                cursor_x, cursor_y = pyautogui.position()
                screenshot.paste(cursor_img, (cursor_x, cursor_y), cursor_img)
                logger.info("Linux screenshot successfully (with cursor)")
            except Exception as e:
                logger.warning(f"Failed to add cursor to screenshot: {e}")
                logger.info("Linux screenshot successfully (without cursor)")
            
            screenshot.save(output_path)
            return True
            
        except Exception as e:
            logger.error(f"Linux screenshot failed: {e}")
            return False
    
    def activate_window(self, window_name: str, strict: bool = False, by_class: bool = False) -> Dict[str, Any]:
        """
        Activate window (Linux uses wmctrl)
        
        Args:
            window_name: Window name
            strict: Whether to strictly match
            by_class: Whether to match by class name
            
        Returns:
            Result dictionary
        """
        try:
            # Build wmctrl command
            flags = f"-{'x' if by_class else ''}{'F' if strict else ''}a"
            cmd = ["wmctrl", flags, window_name]
            
            subprocess.run(cmd, check=True, timeout=5)
            logger.info(f"Linux window activated successfully: {window_name}")
            return {'status': 'success', 'message': 'Window activated'}
            
        except subprocess.CalledProcessError as e:
            logger.warning(f"wmctrl command execution failed: {e}")
            return {'status': 'error', 'message': f'Window {window_name} not found or wmctrl failed'}
        except FileNotFoundError:
            logger.error("wmctrl not installed, please install: sudo apt install wmctrl")
            return {'status': 'error', 'message': 'wmctrl not installed'}
        except Exception as e:
            logger.error(f"Linux window activation failed: {e}")
            return {'status': 'error', 'message': str(e)}
    
    def close_window(self, window_name: str, strict: bool = False, by_class: bool = False) -> Dict[str, Any]:
        """
        Close window (Linux uses wmctrl)
        
        Args:
            window_name: Window name
            strict: Whether to strictly match
            by_class: Whether to match by class name
            
        Returns:
            Result dictionary
        """
        try:
            # Build wmctrl command
            flags = f"-{'x' if by_class else ''}{'F' if strict else ''}c"
            cmd = ["wmctrl", flags, window_name]
            
            subprocess.run(cmd, check=True, timeout=5)
            logger.info(f"Linux window closed successfully: {window_name}")
            return {'status': 'success', 'message': 'Window closed'}
            
        except subprocess.CalledProcessError as e:
            logger.warning(f"wmctrl command execution failed: {e}")
            return {'status': 'error', 'message': f'Window {window_name} not found or wmctrl failed'}
        except FileNotFoundError:
            logger.error("wmctrl not installed")
            return {'status': 'error', 'message': 'wmctrl not installed'}
        except Exception as e:
            logger.error(f"Linux window close failed: {e}")
            return {'status': 'error', 'message': str(e)}
    
    def get_accessibility_tree(self, max_depth: int = 10, max_width: int = 50) -> Dict[str, Any]:
        """
        Get Linux accessibility tree (using AT-SPI)
        
        Args:
            max_depth: Maximum depth
            max_width: Maximum number of child elements per level
            
        Returns:
            Accessibility tree data
        """
        if not LINUX_LIBS_AVAILABLE:
            return {'error': 'Linux accessibility libraries not available'}
        
        try:
            # Get desktop root node
            desktop = pyatspi.Registry.getDesktop(0)
            
            # Serialize accessibility tree
            tree = self._serialize_atspi_element(
                desktop, 
                depth=0, 
                max_depth=max_depth,
                max_width=max_width
            )
            
            return {
                'tree': tree,
                'platform': 'Linux'
            }
            
        except Exception as e:
            logger.error(f"Linux get accessibility tree failed: {e}")
            return {'error': str(e)}
    
    def _serialize_atspi_element(
        self, 
        element: Accessible, 
        depth: int = 0, 
        max_depth: int = 10,
        max_width: int = 50
    ) -> Optional[Dict[str, Any]]:
        """
        Serialize AT-SPI element to dictionary
        
        Args:
            element: AT-SPI accessible element
            depth: Current depth
            max_depth: Maximum depth
            max_width: Maximum width
            
        Returns:
            Serialized dictionary
        """
        if depth > max_depth:
            return None
        
        try:
            result = {
                'depth': depth,
                'role': element.getRoleName(),
                'name': element.name,
            }
            
            # Get states
            try:
                states = element.getState().get_states()
                result['states'] = [StateType._enum_lookup[st].split('_', 1)[1].lower() 
                                   for st in states if st in StateType._enum_lookup]
            except:
                result['states'] = []
            
            # Get attributes
            try:
                attributes = element.get_attributes()
                if attributes:
                    result['attributes'] = dict(attributes)
            except:
                result['attributes'] = {}
            
            # Get position and size (if visible)
            if STATE_SHOWING in element.getState().get_states():
                try:
                    component = element.queryComponent()
                    bbox = component.getExtents(pyatspi.XY_SCREEN)
                    result['position'] = {'x': bbox[0], 'y': bbox[1]}
                    result['size'] = {'width': bbox[2], 'height': bbox[3]}
                except:
                    pass
            
            # Get text content
            try:
                text_obj = element.queryText()
                text = text_obj.getText(0, text_obj.characterCount)
                if text:
                    result['text'] = text.replace("\ufffc", "").replace("\ufffd", "")
            except:
                pass
            
            # Recursively get child elements
            result['children'] = []
            try:
                child_count = min(element.childCount, max_width)
                for i in range(child_count):
                    try:
                        child = element.getChildAtIndex(i)
                        child_data = self._serialize_atspi_element(
                            child, 
                            depth + 1, 
                            max_depth,
                            max_width
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
    
    def get_screen_size(self) -> Dict[str, int]:
        """
        Get screen size
        
        Returns:
            Screen size dictionary
        """
        try:
            if LINUX_LIBS_AVAILABLE:
                d = display.Display()
                screen = d.screen()
                return {
                    'width': screen.width_in_pixels,
                    'height': screen.height_in_pixels
                }
            else:
                # Use pyautogui as fallback
                size = pyautogui.size()
                return {'width': size.width, 'height': size.height}
                
        except Exception as e:
            logger.error(f"Failed to get screen size: {e}")
            return {'width': 1920, 'height': 1080}  # Default value
    
    def list_windows(self) -> List[Dict[str, Any]]:
        """
        List all windows
        
        Returns:
            Window list
        """
        try:
            result = subprocess.run(
                ['wmctrl', '-l'],
                capture_output=True,
                text=True,
                check=True
            )
            
            windows = []
            for line in result.stdout.strip().split('\n'):
                if line:
                    parts = line.split(None, 3)
                    if len(parts) >= 4:
                        windows.append({
                            'id': parts[0],
                            'desktop': parts[1],
                            'hostname': parts[2],
                            'title': parts[3]
                        })
            
            return windows
            
        except FileNotFoundError:
            logger.error("wmctrl not installed")
            return []
        except Exception as e:
            logger.error(f"List windows failed: {e}")
            return []
    
    def get_terminal_output(self) -> Optional[str]:
        """
        Get terminal output (GNOME Terminal)
        
        Returns:
            Terminal output content
        """
        if not LINUX_LIBS_AVAILABLE:
            return None
        
        try:
            desktop = pyatspi.Registry.getDesktop(0)
            
            # Find gnome-terminal-server
            for app in desktop:
                if app.getRoleName() == "application" and app.name == "gnome-terminal-server":
                    for frame in app:
                        if frame.getRoleName() == "frame" and frame.getState().contains(pyatspi.STATE_ACTIVE):
                            # Find terminal component
                            for component in self._find_terminals(frame):
                                try:
                                    text_obj = component.queryText()
                                    output = text_obj.getText(0, text_obj.characterCount)
                                    return output.rstrip() if output else None
                                except:
                                    continue
            
            return None
            
        except Exception as e:
            logger.error(f"Failed to get terminal output: {e}")
            return None
    
    def _find_terminals(self, element) -> List[Accessible]:
        """Recursively find terminal components"""
        terminals = []
        try:
            if element.getRoleName() == "terminal":
                terminals.append(element)
            
            for i in range(element.childCount):
                child = element.getChildAtIndex(i)
                terminals.extend(self._find_terminals(child))
        except:
            pass
        
        return terminals
    
    def set_wallpaper(self, image_path: str) -> Dict[str, Any]:
        """
        Set desktop wallpaper (GNOME)
        
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
            
            # Use gsettings to set wallpaper (GNOME)
            subprocess.run([
                'gsettings', 'set', 
                'org.gnome.desktop.background', 
                'picture-uri', 
                f'file://{image_path}'
            ], check=True, timeout=5)
            
            logger.info(f"Linux wallpaper set successfully: {image_path}")
            return {'status': 'success', 'message': 'Wallpaper set successfully'}
            
        except Exception as e:
            logger.error(f"Linux set wallpaper failed: {e}")
            return {'status': 'error', 'message': str(e)}
    
    def get_system_info(self) -> Dict[str, Any]:
        """
        Get Linux system information
        
        Returns:
            System information dictionary
        """
        try:
            # Get distribution information
            try:
                with open('/etc/os-release', 'r') as f:
                    os_info = {}
                    for line in f:
                        if '=' in line:
                            key, value = line.strip().split('=', 1)
                            os_info[key] = value.strip('"')
                distro = os_info.get('PRETTY_NAME', 'Unknown Linux')
            except:
                distro = 'Unknown Linux'
            
            # Get kernel version
            kernel = subprocess.run(
                ['uname', '-r'],
                capture_output=True,
                text=True
            ).stdout.strip()
            
            return {
                'platform': 'Linux',
                'distro': distro,
                'kernel': kernel,
                'available': self.available
            }
            
        except Exception as e:
            logger.error(f"Failed to get system information: {e}")
            return {
                'platform': 'Linux',
                'error': str(e)
            }
    
    def start_recording(self, output_path: str) -> Dict[str, Any]:
        try:
            try:
                subprocess.run(['ffmpeg', '-version'], 
                             capture_output=True, 
                             check=True,
                             timeout=5)
            except (subprocess.CalledProcessError, FileNotFoundError):
                return {
                    'status': 'error',
                    'message': 'ffmpeg not installed. Install with: sudo apt install ffmpeg'
                }
            
            try:
                if LINUX_LIBS_AVAILABLE:
                    from Xlib import display as xdisplay
                    d = xdisplay.Display()
                    screen_width = d.screen().width_in_pixels
                    screen_height = d.screen().height_in_pixels
                else:
                    # use pyautogui as fallback
                    size = pyautogui.size()
                    screen_width = size.width
                    screen_height = size.height
            except:
                screen_width, screen_height = 1920, 1080
            
            command = [
                'ffmpeg',
                '-y',  
                '-f', 'x11grab',
                '-draw_mouse', '1',
                '-s', f'{screen_width}x{screen_height}',  
                '-i', ':0.0',  
                '-c:v', 'libx264',  
                '-preset', 'ultrafast',  
                '-r', '30',  
                output_path
            ]
            
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True
            )
            
            import time
            time.sleep(1)
            
            if process.poll() is not None:
                error_output = process.stderr.read() if process.stderr else "Unknown error"
                return {
                    'status': 'error',
                    'message': f'Failed to start recording: {error_output}'
                }
            
            logger.info(f"Linux recording started: {output_path}")
            return {
                'status': 'success',
                'message': 'Recording started',
                'process': process
            }
            
        except Exception as e:
            logger.error(f"Linux start recording failed: {e}")
            return {
                'status': 'error',
                'message': str(e)
            }
    
    def stop_recording(self, process) -> Dict[str, Any]:
        try:
            import signal
            
            if not process or process.poll() is not None:
                return {
                    'status': 'error',
                    'message': 'No recording in progress'
                }
            
            process.send_signal(signal.SIGINT)
            
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                logger.warning("ffmpeg did not respond to SIGINT, killing process")
                process.kill()
                process.wait()
            
            logger.info("Linux recording stopped successfully")
            return {
                'status': 'success',
                'message': 'Recording stopped'
            }
            
        except Exception as e:
            logger.error(f"Linux stop recording failed: {e}")
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
        try:
            import psutil
            
            apps = []
            seen_names = set()
            
            for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
                try:
                    pinfo = proc.info
                    name = pinfo['name']
                    exe = pinfo['exe']
                    
                    # Skip kernel processes and system daemons
                    if not exe or name.startswith('['):
                        continue
                    
                    # Skip duplicates
                    if name in seen_names:
                        continue
                    
                    seen_names.add(name)
                    
                    apps.append({
                        'name': name,
                        'pid': pinfo['pid'],
                        'path': exe or '',
                        'cmdline': ' '.join(pinfo.get('cmdline', []))
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