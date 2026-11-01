from typing import Optional, List
from enum import Enum
import re


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GRAY = "\033[90m"
    
    GREEN_SOFT = '\033[38;5;78m'
    BLUE_SOFT = '\033[38;5;39m'
    CYAN_SOFT = '\033[38;5;51m'
    YELLOW_SOFT = '\033[38;5;222m'
    RED_SOFT = '\033[38;5;204m'
    MAGENTA_SOFT = '\033[38;5;141m'
    GRAY_SOFT = '\033[38;5;246m'


class BoxStyle(Enum):
    ROUNDED = "rounded"  # Rounded corner box ╭─╮╰╯
    SQUARE = "square"    # Square corner box ┌─┐└┘
    DOUBLE = "double"    # Double line box ╔═╗╚╝
    SIMPLE = "simple"    # Simple box ===


BOX_CHARS = {
    BoxStyle.ROUNDED: {
        'tl': '╭', 'tr': '╮', 'bl': '╰', 'br': '╯',
        'h': '─', 'v': '│'
    },
    BoxStyle.SQUARE: {
        'tl': '┌', 'tr': '┐', 'bl': '└', 'br': '┘',
        'h': '─', 'v': '│'
    },
    BoxStyle.DOUBLE: {
        'tl': '╔', 'tr': '╗', 'bl': '╚', 'br': '╝',
        'h': '═', 'v': '║'
    },
}


def strip_ansi(text: str) -> str:
    """
    Strip ANSI color codes from text
    
    Args:
        text: Text with potential ANSI codes
        
    Returns:
        Clean text without ANSI codes
    """
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)


def colorize(text: str, color: str = '', bold: bool = False) -> str:
    try:
        color_map = {
            'r': Colors.RESET,
            'b': Colors.BOLD,
            'd': Colors.DIM,
            'g': Colors.GREEN_SOFT,
            'bl': Colors.BLUE_SOFT,
            'c': Colors.CYAN_SOFT,
            'y': Colors.YELLOW_SOFT,
            'rd': Colors.RED_SOFT,
            'm': Colors.MAGENTA_SOFT,
            'gr': Colors.GRAY_SOFT,
        }
        
        prefix = Colors.BOLD if bold else ''
        code = color_map.get(color, color)
        return f"{prefix}{code}{text}{Colors.RESET}"
    except:
        return text


class Box:
    def __init__(self, 
                 width: int = 68,
                 style: BoxStyle = BoxStyle.ROUNDED,
                 color: str = 'bl',
                 padding: int = 2):
        
        self.width = width
        self.style = style
        self.color = color
        self.padding = padding
        self.chars = BOX_CHARS.get(style, BOX_CHARS[BoxStyle.ROUNDED])
    
    def top_line(self, indent: int = 2) -> str:
        indent_str = " " * indent
        if self.style == BoxStyle.SIMPLE:
            return colorize(indent_str + "=" * self.width, self.color)
        return colorize(
            indent_str + self.chars['tl'] + self.chars['h'] * self.width + self.chars['tr'],
            self.color
        )
    
    def bottom_line(self, indent: int = 2) -> str:
        indent_str = " " * indent
        if self.style == BoxStyle.SIMPLE:
            return colorize(indent_str + "=" * self.width, self.color)
        return colorize(
            indent_str + self.chars['bl'] + self.chars['h'] * self.width + self.chars['br'],
            self.color
        )
    
    def separator_line(self, indent: int = 2) -> str:
        indent_str = " " * indent
        if self.style == BoxStyle.SIMPLE:
            return colorize(indent_str + "-" * self.width, self.color)
        return colorize(indent_str + " " + self.chars['h'] * self.width, self.color)
    
    def empty_line(self, indent: int = 2) -> str:
        indent_str = " " * indent
        if self.style == BoxStyle.SIMPLE:
            return ""
        return colorize(
            indent_str + self.chars['v'] + " " * self.width + self.chars['v'],
            self.color
        )
    
    def text_line(self, text: str, align: str = 'left', indent: int = 2, text_color: str = '') -> str:
        indent_str = " " * indent
        content_width = self.width - 2 * self.padding
        
        # Strip ANSI codes to get actual display length
        clean_text = strip_ansi(text)
        text_len = len(clean_text)
        
        # Use original text (may contain colors) or apply new color
        display_text = colorize(text, text_color) if text_color else text
        
        if align == 'center':
            left_pad = (content_width - text_len) // 2
            right_pad = content_width - text_len - left_pad
            content = " " * left_pad + display_text + " " * right_pad
        elif align == 'right':
            left_pad = content_width - text_len
            content = " " * left_pad + display_text
        else:  # left
            right_pad = content_width - text_len
            content = display_text + " " * right_pad
        
        if self.style == BoxStyle.SIMPLE:
            return indent_str + " " * self.padding + content
        
        padding_str = " " * self.padding
        return colorize(indent_str + self.chars['v'], self.color) + \
               padding_str + content + padding_str + \
               colorize(self.chars['v'], self.color)
    
    def build(self, 
              title: Optional[str] = None,
              lines: List[str] = None,
              footer: Optional[str] = None,
              indent: int = 2) -> str:
    
        result = []
        
        result.append(self.top_line(indent))
        
        if title:
            result.append(self.empty_line(indent))
            result.append(self.text_line(title, align='center', indent=indent, text_color='c'))
            result.append(self.empty_line(indent))
        
        if lines:
            for line in lines:
                result.append(self.text_line(line, indent=indent))
        
        if footer:
            result.append(self.empty_line(indent))
            result.append(self.text_line(footer, align='center', indent=indent, text_color='gr'))
        
        result.append(self.bottom_line(indent))
        
        return "\n".join(result)


def print_box(title: Optional[str] = None,
              lines: List[str] = None,
              footer: Optional[str] = None,
              width: int = 68,
              style: BoxStyle = BoxStyle.ROUNDED,
              color: str = 'bl',
              indent: int = 2):
  
    box = Box(width=width, style=style, color=color)
    print(box.build(title=title, lines=lines, footer=footer, indent=indent))


def print_banner(title: str,
                 subtitle: Optional[str] = None,
                 width: int = 66,
                 style: BoxStyle = BoxStyle.ROUNDED,
                 color: str = 'bl',
                 indent: int = 2):
  
    box = Box(width=width, style=style, color=color)
    print()
    print(box.top_line(indent))
    print(box.empty_line(indent))
    print(box.text_line(title, align='center', indent=indent, text_color='c'))
    if subtitle:
        print(box.text_line(subtitle, align='center', indent=indent, text_color='gr'))
    print(box.empty_line(indent))
    print(box.bottom_line(indent))
    print()


def print_section(title: str,
                  content: List[str],
                  color: str = 'c',
                  indent: int = 2):
    indent_str = " " * indent
    print(f"\n{indent_str}{colorize('- ' + title, color, bold=True)}")
    for line in content:
        print(f"{indent_str}   {line}")


def print_separator(width: int = 68, color: str = 'bl', indent: int = 2):
    indent_str = " " * indent
    print(colorize(indent_str + "─" * width, color))