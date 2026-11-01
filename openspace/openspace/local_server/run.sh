#!/bin/bash
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/../.." && pwd )"

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 not installed"
    exit 1
fi

# Check if dependencies are installed
if ! python3 -c "import flask" &> /dev/null; then
    echo "Installing dependencies..."
    pip3 install -q -r "$SCRIPT_DIR/requirements.txt" || {
        echo "Failed to install dependencies"
        exit 1
    }
fi

# Set PYTHONPATH and start server
export PYTHONPATH="$PROJECT_ROOT:$PYTHONPATH"
cd "$PROJECT_ROOT"
python3 -m openspace.local_server.main