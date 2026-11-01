import platform
from openspace.utils.logging import Logger
from typing import Dict, Any, Optional

logger = Logger.get_logger(__name__)

platform_name = platform.system()


class AccessibilityHelper:
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
    
    def get_tree(self, max_depth: int = 10) -> Dict[str, Any]:
        if not self.adapter:
            return {
                'error': f'No adapter available for {self.platform}',
                'platform': self.platform
            }
        
        try:
            return self.adapter.get_accessibility_tree(max_depth=max_depth)
        except Exception as e:
            logger.error(f"Failed to get accessibility tree: {e}")
            return {
                'error': str(e),
                'platform': self.platform
            }
    
    def is_available(self) -> bool:
        return self.adapter is not None and hasattr(self.adapter, 'available') and self.adapter.available
    
    def find_element_by_name(self, tree: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
        if not tree or 'tree' not in tree:
            return None
        
        return self._search_tree(tree['tree'], 'name', name)
    
    def find_element_by_role(self, tree: Dict[str, Any], role: str) -> Optional[Dict[str, Any]]:
        if not tree or 'tree' not in tree:
            return None
        
        return self._search_tree(tree['tree'], 'role', role)
    
    def _search_tree(self, node: Dict[str, Any], key: str, value: str) -> Optional[Dict[str, Any]]:
        if not node:
            return None
        
        # Check current node
        if key in node and node[key] == value:
            return node
        
        # Recursively search child nodes
        if 'children' in node:
            for child in node['children']:
                result = self._search_tree(child, key, value)
                if result:
                    return result
        
        return None
    
    def flatten_tree(self, tree: Dict[str, Any]) -> list:
        if not tree or 'tree' not in tree:
            return []
        
        result = []
        self._flatten_node(tree['tree'], result)
        return result
    
    def _flatten_node(self, node: Dict[str, Any], result: list):
        """Recursively flatten nodes"""
        if not node:
            return
        
        # Add current node (remove children)
        node_copy = {k: v for k, v in node.items() if k != 'children'}
        result.append(node_copy)
        
        # Recursively process child nodes
        if 'children' in node:
            for child in node['children']:
                self._flatten_node(child, result)
    
    def get_visible_elements(self, tree: Dict[str, Any]) -> list:
        all_elements = self.flatten_tree(tree)
        
        visible = []
        for element in all_elements:
            if self.platform == "Linux":
                if 'states' in element and 'showing' in element.get('states', []):
                    visible.append(element)
            elif self.platform == "Darwin":
                if element.get('enabled', False):
                    visible.append(element)
            elif self.platform == "Windows":
                if element.get('states', {}).get('is_visible', False):
                    visible.append(element)
        
        return visible
    
    def get_clickable_elements(self, tree: Dict[str, Any]) -> list:
        all_elements = self.flatten_tree(tree)
        
        clickable_roles = [
            'button', 'push-button', 'toggle-button', 'radio-button',
            'link', 'menu-item', 'AXButton', 'AXLink', 'AXMenuItem'
        ]
        
        clickable = []
        for element in all_elements:
            role = element.get('role', '').lower()
            if any(cr in role for cr in clickable_roles):
                clickable.append(element)
        
        return clickable
    
    def get_statistics(self, tree: Dict[str, Any]) -> Dict[str, Any]:
        all_elements = self.flatten_tree(tree)
        
        # Count roles
        roles = {}
        for element in all_elements:
            role = element.get('role', 'unknown')
            roles[role] = roles.get(role, 0) + 1
        
        return {
            'total_elements': len(all_elements),
            'visible_elements': len(self.get_visible_elements(tree)),
            'clickable_elements': len(self.get_clickable_elements(tree)),
            'roles': roles,
            'platform': self.platform
        }

