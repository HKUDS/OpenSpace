import os
import platform
import shlex
import subprocess
import signal
import time
import json
import uuid
from datetime import datetime
from flask import Flask, request, jsonify, send_file, abort
import pyautogui
import threading
from io import BytesIO
import tempfile

from openspace.utils.logging import Logger
from openspace.local_server.utils import AccessibilityHelper, ScreenshotHelper
from openspace.local_server.platform_adapters import get_platform_adapter
from openspace.local_server.health_checker import HealthChecker
from openspace.local_server.feature_checker import FeatureChecker

platform_name = platform.system()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB

pyautogui.PAUSE = 0
if platform_name == "Darwin":
    pyautogui.DARWIN_CATCH_UP_TIME = 0

logger = Logger.get_logger(__name__)

TIMEOUT = 1800
recording_process = None

if platform_name == "Windows":
    recording_path = os.path.join(os.environ.get('TEMP', 'C:\\Temp'), 'recording.mp4')
else:
    recording_path = "/tmp/recording.mp4"

accessibility_helper = AccessibilityHelper()
screenshot_helper = ScreenshotHelper()
platform_adapter = get_platform_adapter()

feature_checker = FeatureChecker(
    platform_adapter=platform_adapter,
    accessibility_helper=accessibility_helper
)


def get_conda_activation_prefix(conda_env: str = None) -> str:
    """
    Generate platform-specific conda activation command prefix
    
    Args:
        conda_env: Conda environment name (e.g., 'myenv')
    
    Returns:
        Activation command prefix string, empty if no conda_env
    """
    if not conda_env:
        return ""
    
    if platform_name == "Windows":
        # Windows: use conda.bat or conda.exe
        # Try common conda installation paths
        conda_paths = [
            os.path.expandvars("%USERPROFILE%\\miniconda3\\Scripts\\activate.bat"),
            os.path.expandvars("%USERPROFILE%\\anaconda3\\Scripts\\activate.bat"),
            "C:\\ProgramData\\Miniconda3\\Scripts\\activate.bat",
            "C:\\ProgramData\\Anaconda3\\Scripts\\activate.bat",
        ]
        
        # Find first existing conda activate script
        activate_script = None
        for path in conda_paths:
            if os.path.exists(path):
                activate_script = path
                break
        
        if activate_script:
            return f'call "{activate_script}" {conda_env} && '
        else:
            # Fallback: assume conda is in PATH
            return f'conda activate {conda_env} && '
    
    else:
        # Linux/macOS: source conda.sh then activate
        conda_paths = [
            os.path.expanduser("~/miniconda3/etc/profile.d/conda.sh"),
            os.path.expanduser("~/anaconda3/etc/profile.d/conda.sh"),
            "/opt/conda/etc/profile.d/conda.sh",
            "/usr/local/miniconda3/etc/profile.d/conda.sh",
            "/usr/local/anaconda3/etc/profile.d/conda.sh",
        ]
        
        # Find first existing conda.sh
        conda_sh = None
        for path in conda_paths:
            if os.path.exists(path):
                conda_sh = path
                break
        
        if conda_sh:
            return f'source "{conda_sh}" && conda activate {conda_env} && '
        else:
            # Fallback: assume conda is already initialized in shell
            return f'conda activate {conda_env} && '


def wrap_script_with_conda(script: str, conda_env: str = None) -> str:
    """
    Wrap script with conda activation command.
    If conda is not available, returns original script without conda activation.
    """
    if not conda_env:
        return script
    
    if platform_name == "Windows":
        activation_prefix = get_conda_activation_prefix(conda_env)
        return f"{activation_prefix}{script}"
    else:
        conda_paths = [
            os.path.expanduser("~/miniconda3/etc/profile.d/conda.sh"),
            os.path.expanduser("~/anaconda3/etc/profile.d/conda.sh"),
            os.path.expanduser("~/opt/anaconda3/etc/profile.d/conda.sh"),
            "/opt/conda/etc/profile.d/conda.sh",
        ]
        
        conda_sh = None
        for path in conda_paths:
            if os.path.exists(path):
                conda_sh = path
                break
        
        if conda_sh:
            # Use bash -i -c to run interactively, or directly source conda.sh
            wrapped_script = f"""#!/bin/bash
# Initialize conda
if [ -f "{conda_sh}" ]; then
    . "{conda_sh}"
    conda activate {conda_env} 2>/dev/null || true
fi

# Run user script
{script}
"""
            return wrapped_script
        else:
            # Conda not found - log warning and execute script directly without conda
            logger.warning(f"Conda environment '{conda_env}' requested but conda not found. Executing with system Python.")
            return script


health_checker = None

@app.route('/', methods=['GET'])
def health_check():
    """Health check interface - return features information"""
    # Get features from health_checker
    if health_checker:
        features = health_checker.get_simple_features_dict()
    else:
        # Initial startup of health_checker may not have been initialized, fallback to feature_checker
        features = feature_checker.check_all_features(use_cache=True)
    
    return jsonify({
        'status': 'ok',
        'service': 'OpenSpace Desktop Server',
        'version': '1.0.0',
        'platform': platform_name,
        'features': features,
        'timestamp': datetime.now().isoformat()
    })

@app.route('/platform', methods=['GET'])
def get_platform():
    info = {
        'system': platform_name,
        'release': platform.release(),
        'version': platform.version(),
        'machine': platform.machine(),
        'processor': platform.processor()
    }
    
    if platform_adapter and hasattr(platform_adapter, 'get_system_info'):
        info.update(platform_adapter.get_system_info())
    
    return jsonify(info)

@app.route('/execute', methods=['POST'])
@app.route('/setup/execute', methods=['POST'])
def execute_command():
    data = request.json
    # The 'command' key in the JSON request should contain the command to be executed.
    shell = data.get('shell', False)
    command = data.get('command', "" if shell else [])
    timeout = data.get('timeout', 120)
    
    if isinstance(command, str) and not shell:
        command = shlex.split(command)
    
    # Expand user directory
    if isinstance(command, list):
        for i, arg in enumerate(command):
            if arg.startswith("~/"):
                command[i] = os.path.expanduser(arg)
    
    try:
        if platform_name == "Windows":
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=shell,
                text=True,
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=shell,
                text=True,
                timeout=timeout,
            )
        
        return jsonify({
            'status': 'success',
            'output': result.stdout,
            'error': result.stderr,
            'returncode': result.returncode
        })
    except subprocess.TimeoutExpired:
        return jsonify({
            'status': 'error',
            'message': f'Command timeout after {timeout} seconds'
        }), 408
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/execute_with_verification', methods=['POST'])
@app.route('/setup/execute_with_verification', methods=['POST'])
def execute_command_with_verification():
    """Execute command and verify the result based on provided verification criteria"""
    data = request.json
    shell = data.get('shell', False)
    command = data.get('command', "" if shell else [])
    verification = data.get('verification', {})
    max_wait_time = data.get('max_wait_time', 10) # Maximum wait time in seconds
    check_interval = data.get('check_interval', 1) # Check interval in seconds
    
    if isinstance(command, str) and not shell:
        command = shlex.split(command)
    
    # Expand user directory
    if isinstance(command, list):
        for i, arg in enumerate(command):
            if arg.startswith("~/"):
                command[i] = os.path.expanduser(arg)
    
    # Execute the main command
    try:
        if platform_name == "Windows":
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=shell,
                text=True,
                timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=shell,
                text=True,
                timeout=120,
            )
        
        # If no verification is needed, return immediately
        if not verification:
            return jsonify({
                'status': 'success',
                'output': result.stdout,
                'error': result.stderr,
                'returncode': result.returncode
            })
        
        # Wait and verify the result
        start_time = time.time()
        while time.time() - start_time < max_wait_time:
            verification_passed = True
            
            # Check window existence if specified
            if 'window_exists' in verification:
                window_name = verification['window_exists']
                try:
                    if platform_name == 'Linux':
                        wmctrl_result = subprocess.run(
                            ['wmctrl', '-l'],
                            capture_output=True,
                            text=True,
                            check=True
                        )
                        if window_name.lower() not in wmctrl_result.stdout.lower():
                            verification_passed = False
                    elif platform_adapter:
                        # Use platform adapter to check window existence
                        windows = platform_adapter.list_windows() if hasattr(platform_adapter, 'list_windows') else []
                        if not any(window_name.lower() in str(w).lower() for w in windows):
                            verification_passed = False
                except:
                    verification_passed = False
            
            # Check command execution if specified
            if 'command_success' in verification:
                verify_cmd = verification['command_success']
                try:
                    verify_result = subprocess.run(
                        verify_cmd,
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    if verify_result.returncode != 0:
                        verification_passed = False
                except:
                    verification_passed = False
            
            if verification_passed:
                return jsonify({
                    'status': 'success',
                    'output': result.stdout,
                    'error': result.stderr,
                    'returncode': result.returncode,
                    'verification': 'passed',
                    'wait_time': time.time() - start_time
                })
            
            time.sleep(check_interval)
        
        # Verification failed
        return jsonify({
            'status': 'verification_failed',
            'output': result.stdout,
            'error': result.stderr,
            'returncode': result.returncode,
            'verification': 'failed',
            'wait_time': max_wait_time
        }), 500
        
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

def _get_machine_architecture() -> str:
    """Get the machine architecture, e.g., x86_64, arm64, aarch64, i386, etc.
    Returns 'amd' for x86/AMD architectures, 'arm' for ARM architectures, or 'unknown'.
    """
    architecture = platform.machine().lower()
    if architecture in ['amd32', 'amd64', 'x86', 'x86_64', 'x86-64', 'x64', 'i386', 'i686']:
        return 'amd'
    elif architecture in ['arm64', 'aarch64', 'aarch32']:
        return 'arm'
    else:
        return 'unknown'

@app.route('/setup/launch', methods=["POST"])
def launch_app():
    data = request.json
    shell = data.get("shell", False)
    command = data.get("command", "" if shell else [])
    
    if isinstance(command, str) and not shell:
        command = shlex.split(command)
    
    # Expand user directory
    if isinstance(command, list):
        for i, arg in enumerate(command):
            if arg.startswith("~/"):
                command[i] = os.path.expanduser(arg)
    
    try:
        # ARM architecture compatibility: replace google-chrome with chromium
        # ARM64 Chrome is not available yet, can only use Chromium
        if isinstance(command, list) and 'google-chrome' in command and _get_machine_architecture() == 'arm':
            index = command.index('google-chrome')
            command[index] = 'chromium'
            logger.info("ARM architecture detected: replacing 'google-chrome' with 'chromium'")
        
        subprocess.Popen(command, shell=shell)
        cmd_str = command if shell else " ".join(command)
        logger.info(f"Application launched successfully: {cmd_str}")
        return jsonify({
            'status': 'success',
            'message': f'{cmd_str} launched successfully'
        })
    except Exception as e:
        logger.error(f"Application launch failed: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route("/run_python", methods=['POST'])
def run_python():
    data = request.json
    code = data.get('code', None)
    timeout = data.get('timeout', 30)
    working_dir = data.get('working_dir', None)
    env = data.get('env', None)
    conda_env = data.get('conda_env', None)
    
    if not code:
        return jsonify({'status': 'error', 'message': 'Code not supplied!'}), 400
    
    # Generate unique filename
    if platform_name == "Windows":
        temp_filename = os.path.join(tempfile.gettempdir(), f"python_exec_{uuid.uuid4().hex}.py")
    else:
        temp_filename = f"/tmp/python_exec_{uuid.uuid4().hex}.py"
    
    try:
        with open(temp_filename, 'w') as f:
            f.write(code)
        
        # Prepare environment variables
        exec_env = os.environ.copy()
        if env:
            exec_env.update(env)
        
        # If conda_env is specified, try to use bash/cmd to activate and run
        # If conda is not available, fall back to system Python
        if conda_env:
            activation_cmd = get_conda_activation_prefix(conda_env)
            # Check if conda activation command is empty (conda not found)
            if not activation_cmd:
                logger.warning(f"Conda environment '{conda_env}' requested but conda not found. Using system Python.")
                conda_env = None  # Disable conda and use default path
        
        if conda_env and get_conda_activation_prefix(conda_env):
            if platform_name == "Windows":
                # Windows: use cmd with activation
                activation_cmd = get_conda_activation_prefix(conda_env)
                full_cmd = f'{activation_cmd}python "{temp_filename}"'
                result = subprocess.run(
                    ['cmd', '/c', full_cmd],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout,
                    cwd=working_dir or os.getcwd(),
                    env=exec_env
                )
            else:
                # Linux/macOS: use bash with activation
                activation_cmd = get_conda_activation_prefix(conda_env)
                full_cmd = f'{activation_cmd}python3 "{temp_filename}"'
                result = subprocess.run(
                    ['/bin/bash', '-c', full_cmd],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout,
                    cwd=working_dir or os.getcwd(),
                    env=exec_env
                )
        else:
            # No conda activation needed
            python_cmd = 'python' if platform_name == "Windows" else 'python3'
            result = subprocess.run(
                [python_cmd, temp_filename],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                cwd=working_dir or os.getcwd(),
                env=exec_env
            )
        
        os.remove(temp_filename)
        
        output = result.stdout + result.stderr
        
        return jsonify({
            'status': 'success' if result.returncode == 0 else 'error',
            'content': output or "Code executed successfully (no output)",
            'returncode': result.returncode
        })
        
    except subprocess.TimeoutExpired:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)
        return jsonify({
            'status': 'error',
            'message': f'Execution timeout after {timeout} seconds'
        }), 408
    except Exception as e:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route("/run_bash_script", methods=['POST'])
def run_bash_script():
    data = request.json
    script = data.get('script', None)
    timeout = data.get('timeout', 30)
    working_dir = data.get('working_dir', None)
    env = data.get('env', None)
    conda_env = data.get('conda_env', None)
    
    if not script:
        return jsonify({'status': 'error', 'message': 'Script not supplied!'}), 400
    
    # Generate unique filename
    if platform_name == "Windows":
        temp_filename = os.path.join(tempfile.gettempdir(), f"bash_exec_{uuid.uuid4().hex}.sh")
    else:
        temp_filename = f"/tmp/bash_exec_{uuid.uuid4().hex}.sh"
    
    try:
        # Wrap script with conda activation if needed
        final_script = wrap_script_with_conda(script, conda_env)
        
        with open(temp_filename, 'w') as f:
            f.write(final_script)
        
        os.chmod(temp_filename, 0o755)
        
        if platform_name == "Windows":
            shell_cmd = ['bash', temp_filename]
        else:
            shell_cmd = ['/bin/bash', temp_filename]
        
        # Prepare environment variables
        exec_env = os.environ.copy()
        if env:
            exec_env.update(env)
        
        result = subprocess.run(
            shell_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            cwd=working_dir or os.getcwd(),
            env=exec_env
        )
        
        os.unlink(temp_filename)
        
        return jsonify({
            'status': 'success' if result.returncode == 0 else 'error',
            'output': result.stdout,
            'error': "",
            'returncode': result.returncode
        })
        
    except subprocess.TimeoutExpired:
        if os.path.exists(temp_filename):
            os.unlink(temp_filename)
        return jsonify({
            'status': 'error',
            'output': f'Script execution timed out after {timeout} seconds',
            'error': "",
            'returncode': -1
        }), 500
    except Exception as e:
        if os.path.exists(temp_filename):
            try:
                os.unlink(temp_filename)
            except:
                pass
        return jsonify({
            'status': 'error',
            'output': f'Failed to execute script: {str(e)}',
            'error': "",
            'returncode': -1
        }), 500
        
@app.route('/screenshot', methods=['GET'])
def capture_screen_with_cursor():
    """Capture screenshot (including mouse cursor)"""
    try:
        buf = BytesIO()
        tmp_path = os.path.join(tempfile.gettempdir(), f"screenshot_{uuid.uuid4().hex}.png")
        if screenshot_helper.capture(tmp_path, with_cursor=True):
            with open(tmp_path, 'rb') as f:
                buf.write(f.read())
            os.remove(tmp_path)            
            buf.seek(0)
            return send_file(buf, mimetype='image/png')
        else:
            return jsonify({'status':'error','message':'Screenshot failed'}), 500
        
    except Exception as e:
        logger.error(f"Screenshot failed: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/cursor_position', methods=['GET'])
def get_cursor_position():
    """Get cursor position"""
    try:
        x, y = screenshot_helper.get_cursor_position()
        return jsonify({'x': x, 'y': y, 'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/screen_size', methods=['POST', 'GET'])
def get_screen_size():
    """Get screen size"""
    try:
        width, height = screenshot_helper.get_screen_size()
        return jsonify({'width': width, 'height': height, 'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# Accessibility Tree
@app.route("/accessibility", methods=["GET"])
def get_accessibility_tree():
    """Get accessibility tree"""
    try:
        max_depth = request.args.get('max_depth', 10, type=int)
        tree = accessibility_helper.get_tree(max_depth=max_depth)
        return jsonify(tree)
    except Exception as e:
        logger.error(f"Failed to get accessibility tree: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

# File Operations
@app.route('/list_directory', methods=['POST'])
def list_directory():
    """List directory contents"""
    data = request.json
    path = data.get('path', '.')
    
    try:
        path = os.path.expanduser(path)
        items = []
        
        for item in os.listdir(path):
            item_path = os.path.join(path, item)
            items.append({
                'name': item,
                'is_dir': os.path.isdir(item_path),
                'is_file': os.path.isfile(item_path),
                'size': os.path.getsize(item_path) if os.path.isfile(item_path) else None
            })
        
        return jsonify({
            'status': 'success',
            'path': path,
            'items': items
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/file', methods=['POST'])
def file_operation():
    """File operations"""
    data = request.json
    operation = data.get('operation', 'read')
    path = data.get('path')
    
    if not path:
        return jsonify({'status': 'error', 'message': 'Path required'}), 400
    
    path = os.path.expanduser(path)
    
    try:
        if operation == 'read':
            with open(path, 'r') as f:
                content = f.read()
            return jsonify({
                'status': 'success',
                'content': content
            })
        elif operation == 'exists':
            exists = os.path.exists(path)
            return jsonify({
                'status': 'success',
                'exists': exists
            })
        else:
            return jsonify({
                'status': 'error',
                'message': f'Unknown operation: {operation}'
            }), 400
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/desktop_path', methods=['POST', 'GET'])
def get_desktop_path():
    """Get desktop path"""
    try:
        desktop = os.path.expanduser("~/Desktop")
        return jsonify({
            'status': 'success',
            'path': desktop
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route("/setup/activate_window", methods=['POST'])
def activate_window():
    """Activate window"""
    data = request.json
    window_name = data.get("window_name")
    strict = data.get("strict", False)
    by_class_name = data.get("by_class", False)
    
    if not window_name:
        return jsonify({'status': 'error', 'message': 'window_name required'}), 400
    
    try:
        if platform_adapter and hasattr(platform_adapter, 'activate_window'):
            result = platform_adapter.activate_window(window_name, strict=strict)
            if result['status'] == 'success':
                return jsonify(result)
            else:
                return jsonify(result), 400
        else:
            return jsonify({
                'status': 'error',
                'message': f'Window activation not supported on {platform_name}'
            }), 501
    except Exception as e:
        logger.error(f"Window activation failed: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route("/setup/close_window", methods=["POST"])
def close_window():
    """Close window"""
    data = request.json
    window_name = data.get("window_name")
    strict = data.get("strict", False)
    by_class_name = data.get("by_class", False)
    
    if not window_name:
        return jsonify({'status': 'error', 'message': 'window_name required'}), 400
    
    try:
        if platform_adapter and hasattr(platform_adapter, 'close_window'):
            result = platform_adapter.close_window(window_name, strict=strict)
            if result['status'] == 'success':
                return jsonify(result)
            else:
                return jsonify(result), 404
        else:
            return jsonify({
                'status': 'error',
                'message': f'Window closing not supported on {platform_name}'
            }), 501
    except Exception as e:
        logger.error(f"Window closing failed: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/window_size', methods=['POST'])
def get_window_size():
    """Get window size"""
    try:
        width, height = screenshot_helper.get_screen_size()
        return jsonify({
            'status': 'success',
            'width': width,
            'height': height
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/wallpaper', methods=['POST'])
@app.route('/setup/change_wallpaper', methods=['POST'])
def set_wallpaper():
    """Set wallpaper"""
    data = request.json
    image_path = data.get('path')
    
    if not image_path:
        return jsonify({'status': 'error', 'message': 'path required'}), 400
    
    try:
        if platform_adapter and hasattr(platform_adapter, 'set_wallpaper'):
            result = platform_adapter.set_wallpaper(image_path)
            if result['status'] == 'success':
                return jsonify(result)
            else:
                return jsonify(result), 400
        else:
            return jsonify({
                'status': 'error',
                'message': f'Wallpaper setting not supported on {platform_name}'
            }), 501
    except Exception as e:
        logger.error(f"Failed to set wallpaper: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# Screen Recording
@app.route('/start_recording', methods=['POST'])
def start_recording():
    """Start screen recording (supports Linux, macOS, Windows)"""
    global recording_process
    
    # Check if platform adapter supports recording
    if not platform_adapter or not hasattr(platform_adapter, 'start_recording'):
        return jsonify({
            'status': 'error',
            'message': f'Recording not supported on {platform_name}'
        }), 501
    
    # Check if recording is already in progress
    if recording_process and recording_process.poll() is None:
        return jsonify({
            'status': 'error',
            'message': 'Recording is already in progress.'
        }), 400
    
    # Clean up old recording file
    if os.path.exists(recording_path):
        try:
            os.remove(recording_path)
        except OSError as e:
            logger.error(f"Cannot delete old recording file: {e}")
    
    try:
        # Use platform adapter to start recording
        result = platform_adapter.start_recording(recording_path)
        
        if result['status'] == 'success':
            recording_process = result.get('process')
            logger.info("Recording started successfully")
            return jsonify({
                'status': 'success',
                'message': 'Recording started'
            })
        else:
            logger.error(f"Failed to start recording: {result.get('message', 'Unknown error')}")
            return jsonify({
                'status': 'error',
                'message': result.get('message', 'Failed to start recording')
            }), 500
            
    except Exception as e:
        logger.error(f"Failed to start recording: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/end_recording', methods=['POST'])
def end_recording():
    """End screen recording (supports Linux, macOS, Windows)"""
    global recording_process
    
    # Check if recording is in progress
    if not recording_process or recording_process.poll() is not None:
        recording_process = None
        return jsonify({
            'status': 'error',
            'message': 'No recording in progress'
        }), 400
    
    try:
        # Use platform adapter to stop recording
        if platform_adapter and hasattr(platform_adapter, 'stop_recording'):
            result = platform_adapter.stop_recording(recording_process)
            recording_process = None
            
            if result['status'] != 'success':
                logger.error(f"Failed to stop recording: {result.get('message', 'Unknown error')}")
                return jsonify(result), 500
        else:
            # Fallback: terminate process directly
            recording_process.send_signal(signal.SIGINT)
            try:
                recording_process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                logger.warning("ffmpeg not responding, force terminating")
                recording_process.kill()
                recording_process.wait()
            recording_process = None
        
        # Check if recording file exists
        # wait for ffmpeg to write the file header
        for _ in range(10):
            if os.path.exists(recording_path) and os.path.getsize(recording_path) > 0:
                break
            time.sleep(0.5)

        if os.path.exists(recording_path) and os.path.getsize(recording_path) > 0:
            logger.info("Recording ended, file saved")
            return send_file(recording_path, as_attachment=True)
        else:
            logger.error("Recording file is missing or empty")
            return abort(500, description="Recording file is missing or empty")
            
    except Exception as e:
        logger.error(f"Failed to end recording: {str(e)}")
        if recording_process:
            try:
                recording_process.kill()
                recording_process.wait()
            except:
                pass
            recording_process = None
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/terminal', methods=['GET'])
def get_terminal_output():
    """Get terminal output (supports Linux, macOS, Windows)"""
    try:
        if platform_adapter and hasattr(platform_adapter, 'get_terminal_output'):
            output = platform_adapter.get_terminal_output()
            if output:
                return jsonify({'output': output, 'status': 'success'})
            else:
                return jsonify({
                    'status': 'error',
                    'message': f'No terminal output available on {platform_name}',
                    'platform_note': 'Make sure a terminal window is open and active'
                }), 404
        else:
            return jsonify({
                'status': 'error',
                'message': f'Terminal output not supported on {platform_name}'
            }), 501
    except Exception as e:
        logger.error(f"Failed to get terminal output: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route("/setup/upload", methods=["POST"])
def upload_file():
    """Upload file"""
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file provided'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No file selected'}), 400
    
    try:
        # Get target path
        target_path = request.form.get('path', os.path.expanduser('~/Desktop'))
        target_path = os.path.expanduser(target_path)
        
        # Ensure directory exists
        os.makedirs(target_path, exist_ok=True)
        
        # Save file
        file_path = os.path.join(target_path, file.filename)
        file.save(file_path)
        
        logger.info(f"File uploaded successfully: {file_path}")
        return jsonify({
            'status': 'success',
            'path': file_path,
            'message': 'File uploaded successfully'
        })
    except Exception as e:
        logger.error(f"File upload failed: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route("/setup/download_file", methods=["POST"])
def download_file():
    """Download file"""
    data = request.json
    path = data.get('path')
    
    if not path:
        return jsonify({'status': 'error', 'message': 'path required'}), 400
    
    try:
        path = os.path.expanduser(path)
        
        if not os.path.exists(path):
            return jsonify({'status': 'error', 'message': f'File not found: {path}'}), 404
        
        return send_file(path, as_attachment=True)
    except Exception as e:
        logger.error(f"File download failed: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route("/setup/open_file", methods=['POST'])
def open_file():
    """Open file (using system default application)"""
    data = request.json
    path = data.get('path')
    
    if not path:
        return jsonify({'status': 'error', 'message': 'path required'}), 400
    
    try:
        path = os.path.expanduser(path)
        
        if not os.path.exists(path):
            return jsonify({'status': 'error', 'message': f'File not found: {path}'}), 404
        
        if platform_name == "Darwin":
            subprocess.Popen(['open', path])
        elif platform_name == "Linux":
            subprocess.Popen(['xdg-open', path])
        elif platform_name == "Windows":
            os.startfile(path)
        
        logger.info(f"File opened successfully: {path}")
        return jsonify({
            'status': 'success',
            'message': f'File opened: {path}'
        })
    except Exception as e:
        logger.error(f"File opening failed: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

def print_banner(host: str = "127.0.0.1", port: int = 5000, debug: bool = False):
    """Print startup banner with server information"""
    from openspace.utils.display import print_banner as display_banner, print_section, print_separator, colorize
    
    # STARTUP INFORMATION
    display_banner("OpenSpace · Local Server")
    
    server_url = f"http://{host}:{port}"
    
    # Server section
    info_lines = [
        colorize(server_url, 'g', bold=True),
    ]
    if host == '0.0.0.0':
        info_lines.append(f"{colorize('Listening on all interfaces', 'gr')} {colorize('(0.0.0.0:' + str(port) + ')', 'y')}")
    info_lines.append(f"{colorize(platform_name, 'gr')} · {colorize('Debug' if debug else 'Production', 'y' if debug else 'g')}")
    
    print_section("Server", info_lines)
    
    print()
    print_separator()
    print(f"  {colorize('Press Ctrl+C to stop', 'gr')}")
    print()

def run_health_check_async():
    """Asynchronous running health check"""
    def _run():
        from openspace.utils.display import colorize
        time.sleep(2)
        
        print(colorize("\n  - Starting health check...\n", 'c', bold=True))
        
        results = health_checker.check_all(test_endpoints=True)
        
        health_checker.print_results(results, show_endpoint_details=False)
        
        summary = health_checker.get_summary()
        logger.info(f"Health check completed: {summary['fully_available']}/{summary['total']} fully available")
    
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

def run_server(host: str = "127.0.0.1", port: int = 5000, debug: bool = False):
    """
    Start desktop control server
    
    Args:
        host: Listening address (127.0.0.1 for local, 0.0.0.0 for all interfaces)
        port: Listening port
        debug: Debug mode (display detailed logs)
    """
    global health_checker
    
    # Initialize health_checker
    base_url = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}"
    health_checker = HealthChecker(feature_checker, base_url, auto_cleanup=False)
    
    print_banner(host, port, debug)

    if not debug:
        run_health_check_async()
    
    app.run(host=host, port=port, debug=debug, threaded=True)

def main():
    import argparse
    from openspace.config.utils import get_config_value
    
    parser = argparse.ArgumentParser(
        description='OpenSpace Local Server - Desktop Control Server'
    )
    parser.add_argument('--host', type=str, default='127.0.0.1',
                       help='Server host (default: 127.0.0.1)')
    parser.add_argument('--port', type=int, default=5000,
                       help='Server port (default: 5000)')
    parser.add_argument('--debug', action='store_true',
                       help='Enable debug mode')
    parser.add_argument('--config', type=str,
                       help='Path to config.json file')
    
    args = parser.parse_args()
    
    config_path = args.config
    if not config_path:
        config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                server_config = get_config_value(config, 'server', {})
                
                host = args.host if args.host != '127.0.0.1' else get_config_value(server_config, 'host', '127.0.0.1')
                port = args.port if args.port != 5000 else get_config_value(server_config, 'port', 5000)
                debug = args.debug or get_config_value(server_config, 'debug', False)
                
                run_server(host=host, port=port, debug=debug)
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            run_server(host=args.host, port=args.port, debug=args.debug)
    else:
        run_server(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()