import subprocess
import os
from typing import Dict, Any, Optional, List
from openspace.utils.logging import Logger

try:
    import AppKit
    MACOS_LIBS_AVAILABLE = True
except ImportError:
    MACOS_LIBS_AVAILABLE = False

try:
    import atomacos
    ATOMACOS_AVAILABLE = True
except ImportError:
    ATOMACOS_AVAILABLE = False

logger = Logger.get_logger(__name__)

_warning_shown = False


class MacOSAdapter:
    def __init__(self):
        global _warning_shown
        if not MACOS_LIBS_AVAILABLE and not _warning_shown:
            logger.warning("macOS libraries are not fully installed, some features may not be available")
            logger.info("To install missing libraries, run: pip install pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-quartz")
            _warning_shown = True
        if not ATOMACOS_AVAILABLE:
            logger.debug("atomacos not installed; accessibility-tree features unavailable (conflicts with pyautogui>=0.9.54)")
        self.available = MACOS_LIBS_AVAILABLE
    
    def capture_screenshot_with_cursor(self, output_path: str) -> bool:
        """
        Capture screenshot with cursor using macOS native screencapture command
        
        Args:
            output_path: Output file path
            
        Returns:
            Whether successful
        """
        try:
            # -C parameter includes cursor, -x disables sound, -m captures main display
            subprocess.run(["screencapture", "-C", "-x", "-m", output_path], check=True)
            logger.info(f"macOS screenshot successfully: {output_path}")
            return True
        except Exception as e:
            logger.error(f"macOS screenshot failed: {e}")
            return False
    
    def activate_window(self, window_name: str, strict: bool = False) -> Dict[str, Any]:
        """
        Activate window (macOS uses AppleScript)
        
        Args:
            window_name: Window name or application name
            strict: Whether to strictly match
            
        Returns:
            Result dictionary
        """
        try:
            # Try to activate application
            script = f'''
            tell application "System Events"
                set appName to "{window_name}"
                try
                    -- Try to activate application by name
                    set frontmost of first process whose name is appName to true
                    return "success"
                on error
                    -- Try to find window by title
                    set foundWindow to false
                    repeat with theProcess in (every process whose visible is true)
                        try
                            tell theProcess
                                repeat with theWindow in windows
                                    if name of theWindow contains appName then
                                        set frontmost of theProcess to true
                                        set foundWindow to true
                                        exit repeat
                                    end if
                                end repeat
                            end tell
                        end try
                        if foundWindow then exit repeat
                    end repeat
                    
                    if foundWindow then
                        return "success"
                    else
                        return "not found"
                    end if
                end try
            end tell
            '''
            
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if "success" in result.stdout:
                logger.info(f"macOS window activated successfully: {window_name}")
                return {'status': 'success', 'message': 'Window activated'}
            else:
                logger.warning(f"macOS window not found: {window_name}")
                return {'status': 'error', 'message': f'Window {window_name} not found'}
                
        except Exception as e:
            logger.error(f"macOS window activation failed: {e}")
            return {'status': 'error', 'message': str(e)}
    
    def close_window(self, window_name: str, strict: bool = False) -> Dict[str, Any]:
        """
        Close window or application (macOS uses AppleScript)
        
        Args:
            window_name: Window name or application name
            strict: Whether to strictly match
            
        Returns:
            Result dictionary
        """
        try:
            # Try to exit application
            script = f'''
            tell application "{window_name}"
                quit
            end tell
            '''
            
            subprocess.run(['osascript', '-e', script], check=True, timeout=5)
            logger.info(f"macOS window/application closed successfully: {window_name}")
            return {'status': 'success', 'message': 'Window/Application closed'}
            
        except subprocess.TimeoutExpired:
            # If timeout, try to force terminate
            try:
                script_force = f'''
                tell application "{window_name}"
                    quit
                end tell
                do shell script "killall '{window_name}'"
                '''
                subprocess.run(['osascript', '-e', script_force], timeout=5)
                logger.info(f"macOS application force closed: {window_name}")
                return {'status': 'success', 'message': 'Application force closed'}
            except Exception as e2:
                logger.error(f"macOS force close failed: {e2}")
                return {'status': 'error', 'message': str(e2)}
                
        except Exception as e:
            logger.error(f"macOS close window failed: {e}")
            return {'status': 'error', 'message': str(e)}
    
    def get_accessibility_tree(self, max_depth: int = 10) -> Dict[str, Any]:
        """
        Get macOS accessibility tree
        
        Args:
            max_depth: Maximum depth
            
        Returns:
            Accessibility tree data
        """
        if not MACOS_LIBS_AVAILABLE:
            return {'error': 'macOS accessibility libraries not available'}
        if not ATOMACOS_AVAILABLE:
            return {'error': 'atomacos is not installed; accessibility-tree features require it'}
        
        try:
            # Get frontmost application
            workspace = AppKit.NSWorkspace.sharedWorkspace()
            active_app = workspace.activeApplication()
            
            if not active_app:
                return {'error': 'No active application'}
            
            app_name = active_app.get('NSApplicationName', 'Unknown')
            bundle_id = active_app.get('NSApplicationBundleIdentifier', '')
            
            logger.info(f"Getting accessibility tree: {app_name} ({bundle_id})")
            
            try:
                if bundle_id:
                    app_ref = atomacos.getAppRefByBundleId(bundle_id)
                else:
                    # If no bundle_id, try to find by name
                    return {'error': 'Cannot find application without bundle ID'}
                
                # Serialize accessibility tree
                tree = self._serialize_ax_element(app_ref, depth=0, max_depth=max_depth)
                
                return {
                    'app_name': app_name,
                    'bundle_id': bundle_id,
                    'tree': tree,
                    'platform': 'macOS'
                }
                
            except Exception as e:
                logger.error(f"Cannot get app reference: {e}")
                return {
                    'error': f'Cannot get app reference: {e}',
                    'app_name': app_name,
                    'bundle_id': bundle_id
                }
                
        except Exception as e:
            logger.error(f"macOS get accessibility tree failed: {e}")
            return {'error': str(e)}
    
    def _serialize_ax_element(self, element, depth: int = 0, max_depth: int = 10) -> Optional[Dict[str, Any]]:
        """
        Serialize macOS accessibility element to dictionary
        
        Args:
            element: AX element
            depth: Current depth
            max_depth: Maximum depth
            
        Returns:
            Serialized dictionary
        """
        if depth > max_depth:
            return None
        
        try:
            result = {
                'depth': depth
            }
            
            # Get common attributes
            try:
                result['role'] = element.AXRole if hasattr(element, 'AXRole') else 'unknown'
            except:
                result['role'] = 'unknown'
            
            try:
                result['title'] = element.AXTitle if hasattr(element, 'AXTitle') else ''
            except:
                result['title'] = ''
            
            try:
                result['description'] = element.AXDescription if hasattr(element, 'AXDescription') else ''
            except:
                result['description'] = ''
            
            try:
                result['value'] = str(element.AXValue) if hasattr(element, 'AXValue') else ''
            except:
                result['value'] = ''
            
            try:
                result['enabled'] = element.AXEnabled if hasattr(element, 'AXEnabled') else False
            except:
                result['enabled'] = False
            
            try:
                result['focused'] = element.AXFocused if hasattr(element, 'AXFocused') else False
            except:
                result['focused'] = False
            
            # Position and size
            try:
                if hasattr(element, 'AXPosition'):
                    pos = element.AXPosition
                    result['position'] = {'x': pos.x, 'y': pos.y}
            except:
                pass
            
            try:
                if hasattr(element, 'AXSize'):
                    size = element.AXSize
                    result['size'] = {'width': size.width, 'height': size.height}
            except:
                pass
            
            # Recursively get child elements (with limit)
            result['children'] = []
            try:
                if hasattr(element, 'AXChildren') and element.AXChildren:
                    for i, child in enumerate(element.AXChildren[:30]):  # Limit to max 30 child elements
                        try:
                            child_data = self._serialize_ax_element(child, depth + 1, max_depth)
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
    
    def get_running_applications(self) -> List[Dict[str, str]]:
        """
        Get list of all running applications
        
        Returns:
            Application list
        """
        try:
            workspace = AppKit.NSWorkspace.sharedWorkspace()
            running_apps = workspace.runningApplications()
            
            apps = []
            for app in running_apps:
                if app.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular:
                    apps.append({
                        'name': app.localizedName() or 'Unknown',
                        'bundle_id': app.bundleIdentifier() or '',
                        'pid': app.processIdentifier(),
                        'active': app.isActive()
                    })
            
            return apps
            
        except Exception as e:
            logger.error(f"Failed to get running applications list: {e}")
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
            
            if not os.path.exists(image_path):
                return {'status': 'error', 'message': f'Image not found: {image_path}'}
            
            # Use AppleScript to set wallpaper
            script = f'''
            tell application "System Events"
                tell every desktop
                    set picture to "{image_path}"
                end tell
            end tell
            '''
            
            subprocess.run(['osascript', '-e', script], check=True, timeout=10)
            logger.info(f"macOS wallpaper set successfully: {image_path}")
            return {'status': 'success', 'message': 'Wallpaper set successfully'}
            
        except Exception as e:
            logger.error(f"macOS set wallpaper failed: {e}")
            return {'status': 'error', 'message': str(e)}
    
    def get_system_info(self) -> Dict[str, Any]:
        """
        Get macOS system information
        
        Returns:
            System information dictionary
        """
        try:
            # Get macOS version
            version = subprocess.run(
                ['sw_vers', '-productVersion'],
                capture_output=True,
                text=True
            ).stdout.strip()
            
            # Get hardware information
            model = subprocess.run(
                ['sysctl', '-n', 'hw.model'],
                capture_output=True,
                text=True
            ).stdout.strip()
            
            return {
                'platform': 'macOS',
                'version': version,
                'model': model,
                'available': self.available
            }
            
        except Exception as e:
            logger.error(f"Failed to get system information: {e}")
            return {
                'platform': 'macOS',
                'error': str(e)
            }
    
    def _detect_screen_device(self) -> str:
        """
        Return the screen device number of avfoundation, like '1:none'
        
        On macOS, ffmpeg -f avfoundation -list_devices true -i "" will list all devices:
        - AVFoundation video devices (usually the camera is [0])
        - AVFoundation audio devices  
        - The screen capture device usually displays as "Capture screen X", numbered from [1]
        """
        try:
            probe = subprocess.run(
                ['ffmpeg', '-f', 'avfoundation', '-list_devices', 'true', '-i', ''],
                stderr=subprocess.PIPE, text=True, timeout=5
            )
            
            # Find all "Capture screen" devices
            screen_devices = []
            for line in probe.stderr.splitlines():
                # Match lines like "[AVFoundation indev @ 0x...] [1] Capture screen 0"
                if 'Capture screen' in line and '[AVFoundation' in line:
                    # Extract device number from square brackets
                    import re
                    # Find pattern like "] [number] Capture screen"
                    match = re.search(r'\]\s*\[(\d+)\]\s*Capture screen', line)
                    if match:
                        device_id = match.group(1)
                        screen_devices.append(device_id)
                        logger.info(f"Found screen capture device: {device_id} - {line.strip()}")
            
            # Use first found screen capture device
            if screen_devices:
                device = f'{screen_devices[0]}:none'
                logger.info(f"Using screen capture device: {device}")
                return device
            else:
                logger.warning("No screen capture device found, using default '1:none'")
                return '1:none'  # Usually screen capture is device 1
                
        except Exception as e:
            logger.warning(f"Failed to detect screen device: {e}, using default '1:none'")
            return '1:none'

    def start_recording(self, output_path: str) -> Dict[str, Any]:
        try:
            # Check if libx264 encoder is available
            result = subprocess.run(
                ['ffmpeg', '-encoders'],
                capture_output=True,
                text=True,
                timeout=5
            )
            has_libx264 = 'libx264' in result.stdout
            
            # Get screen resolution
            try:
                if MACOS_LIBS_AVAILABLE:
                    from AppKit import NSScreen
                    screen = NSScreen.mainScreen()
                    frame = screen.frame()
                    width = int(frame.size.width)
                    height = int(frame.size.height)
                    logger.info(f"Screen resolution: {width}x{height}")
                else:
                    width, height = 1920, 1080
                    logger.info(f"Using default resolution: {width}x{height}")
            except:
                width, height = 1920, 1080
                logger.info(f"Using default resolution: {width}x{height}")
            
            # Detect screen capture device
            screen_dev = self._detect_screen_device()
            logger.info(f"Screen capture device: {screen_dev}")
            
            # Build ffmpeg command
            command = [
                'ffmpeg', '-y',
                '-f', 'avfoundation',
                '-capture_cursor', '1',
                '-capture_mouse_clicks', '1',
                '-framerate', '30',
                '-i', screen_dev,  # Use detected screen device
            ]
            
            if has_libx264:
                command.extend(['-c:v', 'libx264', '-pix_fmt', 'yuv420p'])
                logger.info("Using libx264 encoder")
            else:
                command.extend(['-c:v', 'mpeg4'])
                logger.info("Using mpeg4 encoder")
            
            command.extend(['-r', '30', output_path])
            
            logger.info(f"Starting recording with command: {' '.join(command)}")
            
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,  
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True
            )
            
            import time
            time.sleep(1.5)  # Wait for a longer time to ensure ffmpeg starts

            # Check if process exited early
            if process.poll() is not None:
                err = process.stderr.read() if process.stderr else ""
                logger.error(f"FFmpeg exited early with stderr: {err}")
                
                if "Operation not permitted" in err or "Screen Recording" in err:
                    return {
                        "status": "error",
                        "message": "Screen-recording permission denied. Please grant permission in System Settings → Privacy & Security → Screen Recording."
                    }
                
                # Check if it's a device error
                if "Input/output error" in err or "Invalid argument" in err or "does not exist" in err:
                    return {
                        "status": "error",
                        "message": f"Invalid screen capture device. Please ensure screen recording is enabled. Error: {err[:200]}"
                    }
                
                error_output = err or "Unknown error"
                return {
                    'status': 'error',
                    'message': f'Failed to start recording: {error_output[:300]}'
                }
            
            logger.info(f"macOS recording started successfully: {output_path}")
            return {
                'status': 'success',
                'message': 'Recording started',
                'process': process
            }
            
        except Exception as e:
            logger.error(f"macOS start recording failed: {e}")
            return {
                'status': 'error',
                'message': str(e)
            }
    
    def stop_recording(self, process) -> Dict[str, Any]:
        try:
            import signal
            import time
            
            if not process or process.poll() is not None:
                return {
                    'status': 'error',
                    'message': 'No recording in progress'
                }

            try:
                process.stdin.write('q')
                process.stdin.flush()
                logger.info("Sent 'q' command to ffmpeg")
                
                process.wait(timeout=5)
                logger.info("ffmpeg exited gracefully")
                time.sleep(0.2)   # give ffmpeg time to flush the file
            
            except subprocess.TimeoutExpired:
                logger.warning("ffmpeg did not respond to 'q', trying SIGINT")
                
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=20)
                    logger.info("ffmpeg responded to SIGINT")
                except subprocess.TimeoutExpired:
                    logger.warning("ffmpeg did not respond to SIGINT, killing process")
                    process.kill()
                    process.wait()
            
            except Exception as e:
                logger.warning(f"Failed to send 'q': {e}, trying SIGINT")
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    logger.warning("Killing ffmpeg")
                    process.kill()
                    process.wait()
            
            time.sleep(0.5)
            
            logger.info("macOS recording stopped successfully")
            return {
                'status': 'success',
                'message': 'Recording stopped'
            }
            
        except Exception as e:
            logger.error(f"macOS stop recording failed: {e}")
            return {
                'status': 'error',
                'message': str(e)
            }
    
    def list_windows(self) -> List[Dict[str, Any]]:
        """
        List all windows
        
        Returns:
            Window list
        """
        try:
            # Use AppleScript to get window list
            script = '''
            tell application "System Events"
                set windowList to {}
                repeat with theProcess in (every process whose visible is true)
                    try
                        set processName to name of theProcess
                        tell theProcess
                            repeat with theWindow in windows
                                try
                                    set windowTitle to name of theWindow
                                    set windowInfo to {processName, windowTitle}
                                    set end of windowList to windowInfo
                                end try
                            end repeat
                        end tell
                    end try
                end repeat
                return windowList
            end tell
            '''
            
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            windows = []
            if result.returncode == 0 and result.stdout:
                # Parse AppleScript output: "app1, window1, app2, window2"
                output = result.stdout.strip()
                if output:
                    # AppleScript returns comma-separated list
                    items = [item.strip() for item in output.split(',')]
                    # Group by pairs (app, window)
                    for i in range(0, len(items), 2):
                        if i + 1 < len(items):
                            windows.append({
                                'app_name': items[i],
                                'window_title': items[i + 1]
                            })
            
            return windows
            
        except Exception as e:
            logger.error(f"List windows failed: {e}")
            return []
    
    def get_terminal_output(self) -> Optional[str]:
        """
        Get terminal output (macOS Terminal.app or iTerm2)
        
        Returns:
            Terminal output content
        """
        try:
            # Try to get Terminal.app output first
            script = '''
            tell application "Terminal"
                if (count of windows) > 0 then
                    try
                        set currentTab to selected tab of front window
                        set terminalOutput to contents of currentTab
                        return terminalOutput
                    on error
                        return ""
                    end try
                else
                    return ""
                end if
            end tell
            '''
            
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0 and result.stdout:
                output = result.stdout.strip()
                if output:
                    return output
            
            # Try iTerm2 if Terminal.app failed
            iterm_script = '''
            tell application "iTerm"
                if (count of windows) > 0 then
                    try
                        tell current session of current window
                            set terminalOutput to contents
                            return terminalOutput
                        end tell
                    on error
                        return ""
                    end try
                else
                    return ""
                end if
            end tell
            '''
            
            result = subprocess.run(
                ['osascript', '-e', iterm_script],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0 and result.stdout:
                output = result.stdout.strip()
                if output:
                    return output
            
            return None
            
        except Exception as e:
            logger.error(f"Failed to get terminal output: {e}")
            return None
