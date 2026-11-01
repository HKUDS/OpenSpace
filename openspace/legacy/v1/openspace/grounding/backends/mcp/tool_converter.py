"""
Tool converter for MCP.

This module provides utilities to convert MCP tools to BaseTool instances.
"""

import copy
from typing import Any, Dict
from mcp.types import Tool as MCPTool

from openspace.grounding.core.tool import BaseTool, RemoteTool
from openspace.grounding.core.types import BackendType, ToolSchema
from openspace.grounding.core.transport.connectors import BaseConnector
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


def _sanitize_mcp_schema(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sanitize MCP tool schema to ensure Claude API compatibility (JSON Schema draft 2020-12).
    
    Fixes:
    - Empty schemas -> valid object schema
    - Missing required fields (type, properties, required)
    - Removes non-standard fields (title, examples, nullable, default, etc.)
    - Recursively cleans nested properties and items
    - Ensures every property has a valid type
    - Ensures top-level type is 'object' (Anthropic API requirement)
    """
    if not params:
        return {"type": "object", "properties": {}, "required": []}
    
    sanitized = copy.deepcopy(params)
    sanitized = _deep_sanitize(sanitized)
    
    # Anthropic API requires top-level type to be 'object'
    # If it's not an object, wrap the schema as a property of an object
    top_level_type = sanitized.get("type")
    if top_level_type and top_level_type != "object":
        logger.debug(f"[MCP_SCHEMA_SANITIZE] Wrapping non-object schema (type={top_level_type}) into object")
        wrapped = {
            "type": "object",
            "properties": {
                "value": sanitized  # The original schema becomes a property
            },
            "required": ["value"]  # Make it required
        }
        sanitized = wrapped
    
    return sanitized


def _deep_sanitize(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively sanitize a JSON schema to conform to JSON Schema draft 2020-12.
    Removes non-standard fields and ensures valid structure.
    """
    if not isinstance(schema, dict):
        return {"type": "string"}
    
    # Allowed top-level keys for Claude API compatibility
    allowed_keys = {
        "type", "properties", "required", "items", 
        "description", "enum", "const",
        "minimum", "maximum", "minLength", "maxLength",
        "minItems", "maxItems", "pattern",
        "additionalProperties", "anyOf", "oneOf", "allOf"
    }
    
    # Remove disallowed keys
    keys_to_remove = [k for k in schema if k not in allowed_keys]
    for k in keys_to_remove:
        schema.pop(k, None)
    
    # Ensure type exists
    if "type" not in schema:
        # Type is defined via anyOf/oneOf/allOf - don't add default type
        # These combination keywords define the type themselves
        if "anyOf" in schema or "oneOf" in schema or "allOf" in schema:
            pass  # Type is defined through combination keywords, do not add default type
        # Try to infer type
        elif "properties" in schema:
            schema["type"] = "object"
        elif "items" in schema:
            schema["type"] = "array"
        elif "enum" in schema:
            # For enum, try to infer from values
            enum_vals = schema.get("enum", [])
            if enum_vals and all(isinstance(v, str) for v in enum_vals):
                schema["type"] = "string"
            elif enum_vals and all(isinstance(v, (int, float)) for v in enum_vals):
                schema["type"] = "number"
            else:
                schema["type"] = "string"
        elif not schema:
            # Empty schema (e.g., only had $schema which was removed) -> no parameters needed
            schema["type"] = "object"
            schema["properties"] = {}
            schema["required"] = []
        else:
            schema["type"] = "object"
    
    # Handle object type
    if schema.get("type") == "object":
        if "properties" not in schema:
            schema["properties"] = {}
        if "required" not in schema:
            schema["required"] = []
        
        # Recursively sanitize properties
        if isinstance(schema.get("properties"), dict):
            for prop_name, prop_schema in list(schema["properties"].items()):
                if isinstance(prop_schema, dict):
                    schema["properties"][prop_name] = _deep_sanitize(prop_schema)
                else:
                    # Invalid property schema, replace with string
                    schema["properties"][prop_name] = {"type": "string"}
        
        # Sanitize additionalProperties if present
        if "additionalProperties" in schema and isinstance(schema["additionalProperties"], dict):
            schema["additionalProperties"] = _deep_sanitize(schema["additionalProperties"])
    
    # Handle array type
    elif schema.get("type") == "array":
        if "items" in schema:
            if isinstance(schema["items"], dict):
                schema["items"] = _deep_sanitize(schema["items"])
            elif isinstance(schema["items"], list):
                # Tuple validation - sanitize each item
                schema["items"] = [_deep_sanitize(item) if isinstance(item, dict) else {"type": "string"} for item in schema["items"]]
            else:
                schema["items"] = {"type": "string"}
        else:
            # Default items to string if not specified
            schema["items"] = {"type": "string"}
    
    # Handle anyOf/oneOf/allOf
    for combo_key in ["anyOf", "oneOf", "allOf"]:
        if combo_key in schema and isinstance(schema[combo_key], list):
            schema[combo_key] = [
                _deep_sanitize(sub) if isinstance(sub, dict) else {"type": "string"}
                for sub in schema[combo_key]
            ]
    
    return schema


def convert_mcp_tool_to_base_tool(
    mcp_tool: MCPTool, 
    connector: BaseConnector
) -> BaseTool:
    """
    Convert an MCP Tool to a BaseTool (RemoteTool) instance.
    
    This function extracts the tool schema from an MCP tool object and creates
    a RemoteTool that can be used within the grounding framework.
    
    Args:
        mcp_tool: MCP Tool object from the MCP SDK
        connector: Connector instance for communicating with the MCP server
        
    Returns:
        RemoteTool instance wrapping the MCP tool
    """
    # Extract tool metadata
    tool_name = mcp_tool.name
    tool_description = getattr(mcp_tool, 'description', None) or ""
    
    # Convert MCP input schema to our parameter schema format (with sanitization)
    input_schema: Dict[str, Any] = {}
    if hasattr(mcp_tool, 'inputSchema') and mcp_tool.inputSchema:
        input_schema = _sanitize_mcp_schema(mcp_tool.inputSchema)
    else:
        input_schema = {"type": "object", "properties": {}, "required": []}
    
    # Create ToolSchema
    schema = ToolSchema(
        name=tool_name,
        description=tool_description,
        parameters=input_schema,
        backend_type=BackendType.MCP,
    )
    
    # Create and return RemoteTool
    remote_tool = RemoteTool(
        connector=connector,
        remote_name=tool_name,
        schema=schema,
        backend=BackendType.MCP,
    )
    
    logger.debug(f"Converted MCP tool '{tool_name}' to RemoteTool")
    return remote_tool