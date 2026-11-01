import ast
from radon.complexity import cc_visit
from radon.metrics import h_visit
from radon.raw import analyze
import math
import re


def max_nesting_depth(code_string):
    """Calculate maximum nesting depth for Python code using AST."""

    class NestingVisitor(ast.NodeVisitor):
        def __init__(self):
            self.current_depth = 0
            self.max_depth = 0

        def generic_visit(self, node):
            if isinstance(
                node,
                (
                    ast.If,
                    ast.For,
                    ast.While,
                    ast.With,
                    ast.Try,
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                ),
            ):
                self.current_depth += 1
                self.max_depth = max(self.max_depth, self.current_depth)
                super().generic_visit(node)
                self.current_depth -= 1
            else:
                super().generic_visit(node)

    tree = ast.parse(code_string)
    visitor = NestingVisitor()
    visitor.visit(tree)
    return visitor.max_depth


def analyze_python_complexity(code_string):
    """
    Comprehensive complexity analysis for Python code using radon library.
    Uses AST parsing and advanced metrics like Halstead complexity.

    Args:
        code_string: Python source code to analyze

    Returns:
        Dictionary of complexity metrics

    Raises:
        SyntaxError: If the code cannot be parsed as valid Python
    """
    cc_results = cc_visit(code_string)
    total_cc = sum(block.complexity for block in cc_results)
    avg_cc = total_cc / len(cc_results) if cc_results else 0

    h_metrics = h_visit(code_string)
    halstead_total = h_metrics.total if h_metrics.total else None
    halstead_volume = halstead_total.volume if halstead_total else 1
    halstead_difficulty = halstead_total.difficulty if halstead_total else 0
    halstead_effort = halstead_total.effort if halstead_total else 0

    raw_metrics = analyze(code_string)
    loc = raw_metrics.loc
    lloc = raw_metrics.lloc
    comments = raw_metrics.comments

    mi = (
        171
        - 5.2 * (math.log2(halstead_volume) if halstead_volume > 0 else 0)
        - 0.23 * total_cc
        - 16.2 * (math.log2(loc) if loc > 0 else 0)
    )

    nesting_depth = max_nesting_depth(code_string)

    # Normalized scores for aggregation
    norm_cc = total_cc / 10  # Assuming 10 is high complexity
    norm_halstead = math.log2(halstead_volume + 1) / 10
    norm_loc = math.log2(loc + 1) / 10
    norm_nesting = nesting_depth / 5  # Assuming depth 5 is quite nested

    # Complexity Score (weighted sum)
    complexity_score = (
        0.4 * norm_cc + 0.4 * norm_halstead + 0.1 * norm_loc + 0.1 * norm_nesting
    )

    return {
        "cyclomatic_complexity": total_cc,
        "average_cyclomatic_complexity": avg_cc,
        "halstead_volume": halstead_volume,
        "halstead_difficulty": halstead_difficulty,
        "halstead_effort": halstead_effort,
        "lines_of_code": loc,
        "logical_lines_of_code": lloc,
        "comments": comments,
        "maintainability_index": mi,
        "max_nesting_depth": nesting_depth,
        "complexity_score": round(min(complexity_score, 1.0), 3),
    }


def analyze_cpp_complexity(code_string):
    """
    Simple complexity analysis for C/C++/CUDA code using regex patterns.
    Returns metrics similar to Python analysis but using basic text analysis.

    Args:
        code_string: C/C++/CUDA source code to analyze

    Returns:
        Dictionary of complexity metrics
    """
    lines = code_string.split("\n")

    # Count lines of code (excluding empty lines and comments)
    loc = len(lines)
    lloc = 0
    comments = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if (
            stripped.startswith("//")
            or stripped.startswith("/*")
            or stripped.endswith("*/")
        ):
            comments += 1
        else:
            lloc += 1

    # Simple cyclomatic complexity - count decision points
    complexity_patterns = [
        r"\bif\b",
        r"\belse\b",
        r"\bwhile\b",
        r"\bfor\b",
        r"\bswitch\b",
        r"\bcase\b",
        r"\bcatch\b",
        r"\b\?\b",
    ]

    total_cc = 1  # Base complexity
    for pattern in complexity_patterns:
        total_cc += len(re.findall(pattern, code_string, re.IGNORECASE))

    # Estimate nesting depth by counting braces
    max_nesting = 0
    current_nesting = 0
    for char in code_string:
        if char == "{":
            current_nesting += 1
            max_nesting = max(max_nesting, current_nesting)
        elif char == "}":
            current_nesting = max(0, current_nesting - 1)

    # Simple maintainability index approximation
    volume = max(1, lloc * math.log2(max(1, total_cc)))
    mi = max(
        0,
        171
        - 5.2 * math.log2(max(1, volume))
        - 0.23 * total_cc
        - 16.2 * math.log2(max(1, loc)),
    )

    # Normalized scores
    norm_cc = min(total_cc / 10, 1.0)
    norm_volume = min(math.log2(volume + 1) / 10, 1.0)
    norm_loc = min(math.log2(loc + 1) / 10, 1.0)
    norm_nesting = min(max_nesting / 5, 1.0)

    complexity_score = (
        0.4 * norm_cc + 0.4 * norm_volume + 0.1 * norm_loc + 0.1 * norm_nesting
    )

    return {
        "cyclomatic_complexity": total_cc,
        "average_cyclomatic_complexity": total_cc,  # Same as total for simplicity
        "halstead_volume": volume,
        "halstead_difficulty": 1.0,  # Placeholder
        "halstead_effort": volume,  # Simplified
        "lines_of_code": loc,
        "logical_lines_of_code": lloc,
        "comments": comments,
        "maintainability_index": mi,
        "max_nesting_depth": max_nesting,
        "complexity_score": round(min(complexity_score, 1.0), 3),
    }


def analyze_generic_complexity(code_string):
    """
    Simple line-based complexity analysis for unknown languages.

    Args:
        code_string: Source code in any language

    Returns:
        Dictionary of basic complexity metrics
    """
    lines = code_string.split("\n")
    loc = len([line for line in lines if line.strip()])

    # Very simple complexity estimate based on code length
    complexity_score = min(math.log2(max(1, loc)) / 10, 1.0)

    return {
        "cyclomatic_complexity": 1,
        "average_cyclomatic_complexity": 1,
        "halstead_volume": max(1, loc),
        "halstead_difficulty": 1.0,
        "halstead_effort": max(1, loc),
        "lines_of_code": loc,
        "logical_lines_of_code": loc,
        "comments": 0,
        "maintainability_index": 100.0,  # Default good score
        "max_nesting_depth": 1,
        "complexity_score": round(complexity_score, 3),
    }


def analyze_code_metrics(code_string, language="python"):
    """
    Analyze code complexity metrics for different programming languages.

    This function routes to appropriate analysis methods based on the language:
    - Python: Full AST-based analysis with Halstead metrics
    - C/C++/CUDA: Regex-based pattern matching analysis
    - Other languages: Simple line-based complexity estimation

    Args:
        code_string: The source code to analyze
        language: Programming language ("python", "cpp", "c", "cuda", etc.)

    Returns:
        Dictionary of complexity metrics including:
        - cyclomatic_complexity: Code complexity measure
        - halstead_volume: Code volume metric
        - lines_of_code: Total lines
        - maintainability_index: Code maintainability score
        - complexity_score: Normalized overall complexity (0-1)
    """
    # Normalize language name
    language = language.lower()

    # For Python, use the full radon-based analysis
    if language == "python":
        try:
            return analyze_python_complexity(code_string)
        except SyntaxError:
            # If Python parsing fails, fall back to C++ analysis
            return analyze_cpp_complexity(code_string)

    # For C/C++/CUDA/Go/Rust and other languages, use regex-based analysis
    elif language in ["cpp", "c", "cuda", "c++", "go", "golang", "rust"]:
        return analyze_cpp_complexity(code_string)

    # For unknown languages, use simple line-based complexity
    else:
        return analyze_generic_complexity(code_string)
