"""
GUI Action Space Definitions.
"""
from typing import Dict, Any

# Screen resolution constants
X_MAX = 1920
Y_MAX = 1080

# Keyboard keys constants
KEYBOARD_KEYS = [
    '\t', '\n', '\r', ' ', '!', '"', '#', '$', '%', '&', "'", '(', ')', '*', '+', ',', '-', '.', '/', 
    '0', '1', '2', '3', '4', '5', '6', '7', '8', '9', ':', ';', '<', '=', '>', '?', '@', 
    '[', '\\', ']', '^', '_', '`', 
    'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z', 
    '{', '|', '}', '~', 
    'accept', 'add', 'alt', 'altleft', 'altright', 'apps', 'backspace', 
    'browserback', 'browserfavorites', 'browserforward', 'browserhome', 'browserrefresh', 'browsersearch', 'browserstop', 
    'capslock', 'clear', 'convert', 'ctrl', 'ctrlleft', 'ctrlright', 'decimal', 'del', 'delete', 'divide', 
    'down', 'end', 'enter', 'esc', 'escape', 'execute', 
    'f1', 'f10', 'f11', 'f12', 'f13', 'f14', 'f15', 'f16', 'f17', 'f18', 'f19', 
    'f2', 'f20', 'f21', 'f22', 'f23', 'f24', 'f3', 'f4', 'f5', 'f6', 'f7', 'f8', 'f9', 
    'final', 'fn', 'hanguel', 'hangul', 'hanja', 'help', 'home', 'insert', 'junja', 'kana', 'kanji', 
    'launchapp1', 'launchapp2', 'launchmail', 'launchmediaselect', 'left', 'modechange', 'multiply', 
    'nexttrack', 'nonconvert', 'num0', 'num1', 'num2', 'num3', 'num4', 'num5', 'num6', 'num7', 'num8', 'num9', 
    'numlock', 'pagedown', 'pageup', 'pause', 'pgdn', 'pgup', 'playpause', 'prevtrack', 'print', 'printscreen', 
    'prntscrn', 'prtsc', 'prtscr', 'return', 'right', 'scrolllock', 'select', 'separator', 
    'shift', 'shiftleft', 'shiftright', 'sleep', 'stop', 'subtract', 'tab', 'up', 
    'volumedown', 'volumemute', 'volumeup', 'win', 'winleft', 'winright', 'yen', 
    'command', 'option', 'optionleft', 'optionright'
]

# Action Space Definition
ACTION_SPACE = [
    {
        "action_type": "MOVE_TO",
        "note": "move the cursor to the specified position",
        "parameters": {
            "x": {"type": float, "range": [0, X_MAX], "optional": False},
            "y": {"type": float, "range": [0, Y_MAX], "optional": False},
        }
    },
    {
        "action_type": "CLICK",
        "note": "click the left button if button not specified, otherwise click the specified button",
        "parameters": {
            "button": {"type": str, "range": ["left", "right", "middle"], "optional": True},
            "x": {"type": float, "range": [0, X_MAX], "optional": True},
            "y": {"type": float, "range": [0, Y_MAX], "optional": True},
            "num_clicks": {"type": int, "range": [1, 2, 3], "optional": True},
        }
    },
    {
        "action_type": "MOUSE_DOWN",
        "note": "press the mouse button",
        "parameters": {
            "button": {"type": str, "range": ["left", "right", "middle"], "optional": True}
        }
    },
    {
        "action_type": "MOUSE_UP",
        "note": "release the mouse button",
        "parameters": {
            "button": {"type": str, "range": ["left", "right", "middle"], "optional": True}
        }
    },
    {
        "action_type": "RIGHT_CLICK",
        "note": "right click at position",
        "parameters": {
            "x": {"type": float, "range": [0, X_MAX], "optional": True},
            "y": {"type": float, "range": [0, Y_MAX], "optional": True}
        }
    },
    {
        "action_type": "DOUBLE_CLICK",
        "note": "double click at position",
        "parameters": {
            "x": {"type": float, "range": [0, X_MAX], "optional": True},
            "y": {"type": float, "range": [0, Y_MAX], "optional": True}
        }
    },
    {
        "action_type": "DRAG_TO",
        "note": "drag the cursor to position",
        "parameters": {
            "x": {"type": float, "range": [0, X_MAX], "optional": False},
            "y": {"type": float, "range": [0, Y_MAX], "optional": False}
        }
    },
    {
        "action_type": "SCROLL",
        "note": "scroll the mouse wheel",
        "parameters": {
            "dx": {"type": int, "range": None, "optional": False},
            "dy": {"type": int, "range": None, "optional": False}
        }
    },
    {
        "action_type": "TYPING",
        "note": "type the specified text",
        "parameters": {
            "text": {"type": str, "range": None, "optional": False}
        }
    },
    {
        "action_type": "PRESS",
        "note": "press the specified key",
        "parameters": {
            "key": {"type": str, "range": KEYBOARD_KEYS, "optional": False}
        }
    },
    {
        "action_type": "KEY_DOWN",
        "note": "press down the specified key",
        "parameters": {
            "key": {"type": str, "range": KEYBOARD_KEYS, "optional": False}
        }
    },
    {
        "action_type": "KEY_UP",
        "note": "release the specified key",
        "parameters": {
            "key": {"type": str, "range": KEYBOARD_KEYS, "optional": False}
        }
    },
    {
        "action_type": "HOTKEY",
        "note": "press key combination",
        "parameters": {
            "keys": {"type": list, "range": [KEYBOARD_KEYS], "optional": False}
        }
    },
    {
        "action_type": "WAIT",
        "note": "wait until next action",
    },
    {
        "action_type": "FAIL",
        "note": "mark task as failed",
    },
    {
        "action_type": "DONE",
        "note": "mark task as done",
    }
]


def build_pyautogui_command(action_type: str, parameters: Dict[str, Any]) -> str:
    """
    Build pyautogui command from action type and parameters.
    
    Args:
        action_type: Type of action (e.g., 'CLICK', 'TYPING')
        parameters: Action parameters
    
    Returns:
        Python command string
    """
    if action_type == "MOVE_TO":
        if "x" in parameters and "y" in parameters:
            x, y = parameters["x"], parameters["y"]
            return f"pyautogui.moveTo({x}, {y}, 0.5, pyautogui.easeInQuad)"
        else:
            return "pyautogui.moveTo()"
    
    elif action_type == "CLICK":
        button = parameters.get("button", "left")
        num_clicks = parameters.get("num_clicks", 1)
        
        if "x" in parameters and "y" in parameters:
            x, y = parameters["x"], parameters["y"]
            return f"pyautogui.click(button='{button}', x={x}, y={y}, clicks={num_clicks})"
        else:
            return f"pyautogui.click(button='{button}', clicks={num_clicks})"
    
    elif action_type == "MOUSE_DOWN":
        button = parameters.get("button", "left")
        return f"pyautogui.mouseDown(button='{button}')"
    
    elif action_type == "MOUSE_UP":
        button = parameters.get("button", "left")
        return f"pyautogui.mouseUp(button='{button}')"
    
    elif action_type == "RIGHT_CLICK":
        if "x" in parameters and "y" in parameters:
            x, y = parameters["x"], parameters["y"]
            return f"pyautogui.rightClick(x={x}, y={y})"
        else:
            return "pyautogui.rightClick()"
    
    elif action_type == "DOUBLE_CLICK":
        if "x" in parameters and "y" in parameters:
            x, y = parameters["x"], parameters["y"]
            return f"pyautogui.doubleClick(x={x}, y={y})"
        else:
            return "pyautogui.doubleClick()"
    
    elif action_type == "DRAG_TO":
        if "x" in parameters and "y" in parameters:
            x, y = parameters["x"], parameters["y"]
            return f"pyautogui.dragTo({x}, {y}, 1.0, button='left')"
    
    elif action_type == "SCROLL":
        dx = parameters.get("dx", 0)
        dy = parameters.get("dy", 0)
        return f"pyautogui.scroll({dy})"
    
    elif action_type == "TYPING":
        text = parameters.get("text", "")
        # Use repr() for proper string escaping
        return f"pyautogui.typewrite({repr(text)})"
    
    elif action_type == "PRESS":
        key = parameters.get("key", "")
        return f"pyautogui.press('{key}')"
    
    elif action_type == "KEY_DOWN":
        key = parameters.get("key", "")
        return f"pyautogui.keyDown('{key}')"
    
    elif action_type == "KEY_UP":
        key = parameters.get("key", "")
        return f"pyautogui.keyUp('{key}')"
    
    elif action_type == "HOTKEY":
        keys = parameters.get("keys", [])
        if keys:
            keys_str = ", ".join([f"'{k}'" for k in keys])
            return f"pyautogui.hotkey({keys_str})"
    
    return None