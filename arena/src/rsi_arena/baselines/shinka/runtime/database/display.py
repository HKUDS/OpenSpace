import json
import logging
import time
from typing import Optional, Callable, Any
import rich.box  # type: ignore
import rich  # type: ignore
from rich.columns import Columns as RichColumns  # type: ignore
from rich.console import Console as RichConsole  # type: ignore
from rich.table import Table as RichTable  # type: ignore

logger = logging.getLogger(__name__)


class DatabaseDisplay:
    """Handles rich console display and formatting for database summaries."""

    def __init__(
        self,
        cursor,
        conn,
        config,
        island_manager,
        count_programs_func: Callable[[], int],
        get_best_program_func: Callable[[], Optional[Any]],
        default_console: Optional[RichConsole] = None,
    ):
        self.cursor = cursor
        self.conn = conn
        self.config = config
        self.island_manager = island_manager
        self.count_programs_func = count_programs_func
        self.get_best_program_func = get_best_program_func
        self.default_console = default_console

    def set_default_console(self, console: Optional[RichConsole]) -> None:
        """Set default console used by all rich display methods."""
        self.default_console = console

    def print_program_summary(self, program, console: Optional[RichConsole] = None):
        """Print a rich summary of a newly added program in two rows."""
        _console = console or self.default_console or RichConsole()

        # Get the best program's score
        best_program_overall = self.get_best_program_func()
        best_score_str = "[dim]N/A[/dim]"
        if best_program_overall and best_program_overall.combined_score is not None:
            best_score_val = best_program_overall.combined_score
            if best_score_val is not None:
                best_score_str = f"[bold yellow]{best_score_val:.3f}[/bold yellow]"

        # Calculate total API costs
        total_api_cost = 0
        if self.cursor:
            query = "SELECT metadata FROM programs"
            self.cursor.execute(query)
            for row in self.cursor.fetchall():
                if row["metadata"]:
                    metadata = json.loads(row["metadata"] or "{}")
                    if "api_costs" in metadata:
                        total_api_cost += float(metadata["api_costs"])
                    if "embed_cost" in metadata:
                        total_api_cost += float(metadata["embed_cost"])
                    if "novelty_cost" in metadata:
                        total_api_cost += float(metadata["novelty_cost"])
                    if "meta_cost" in metadata:
                        total_api_cost += float(metadata["meta_cost"])

        # Create a table with headers - include generation and total cost
        table = RichTable(
            title=(
                f"[bold green]Program Evaluation Summary - "
                f"Gen {program.generation} | "
                f"Total Cost: ${total_api_cost:.2f}[/bold green]"
            ),
            border_style="green",
            box=rich.box.ROUNDED,
            show_header=True,
            header_style="bold cyan",
            padding=(0, 1),
            width=120,  # Set fixed width for the table
        )

        # Add columns for each piece of information
        table.add_column(
            "GenID: " + str(program.generation),
            style="cyan",
            justify="center",
            width=12,
        )
        table.add_column("Island", style="magenta", justify="center", width=8)
        table.add_column("Status", style="white", justify="center", width=14)
        table.add_column("Score", style="white", justify="right", width=8)
        table.add_column(
            "Patch Name",
            style="yellow",
            justify="left",
            width=32,
            overflow="ellipsis",
        )
        table.add_column(
            "Type",
            style="yellow",
            justify="left",
            width=6,
            overflow="ellipsis",
        )
        table.add_column("Complex", style="yellow", justify="right", width=7)
        table.add_column("Cost", style="green", justify="right", width=7)
        table.add_column("Time", style="blue", justify="right", width=5)

        # Correctness with color coding
        if program.correct:
            status_display = "[bold green]✓ Correct[/bold green]"
        else:
            status_display = "[bold red]✗ Incorrect[/bold red]"

        # Get combined score with color coding
        score_display = "[dim]N/A[/dim]"
        if program.combined_score is not None:
            score = program.combined_score
            if score > 0.8:
                color = "bold green"
            elif score > 0.5:
                color = "green"
            else:
                color = "yellow"
            score_display = f"[{color}]{score:.3f}[/{color}]"

        # Format cost breakdown
        cost_display = "[dim]N/A[/dim]"
        if program.metadata:
            api_cost = float(program.metadata.get("api_costs", 0))
            embed_cost = float(program.metadata.get("embed_cost", 0))
            novelty_cost = float(program.metadata.get("novelty_cost", 0))
            meta_cost = float(program.metadata.get("meta_cost", 0))
            total_cost = api_cost + embed_cost + novelty_cost + meta_cost
            cost_display = f"${total_cost:.3f}"

        # Format time
        time_display = "[dim]N/A[/dim]"
        if program.metadata:
            time_val = program.metadata.get(
                "compute_time", program.metadata.get("pipeline_seconds")
            )
        else:
            time_val = None
        if time_val is not None:
            if time_val > 60:
                time_display = f"{time_val / 60:.1f}m"
            else:
                time_display = f"{time_val:.1f}s"

        # Add the data row
        island_display = (
            f"I-{program.island_idx}" if program.island_idx is not None else "N/A"
        )
        table.add_row(
            f"Best: {best_score_str}",
            island_display,
            status_display,
            score_display,
            program.metadata.get("patch_name", "[dim]N/A[/dim]")[:30],
            program.metadata.get("patch_type", "[dim]N/A[/dim]"),
            f"{program.complexity:.1f}",
            cost_display,
            time_display,
        )
        _console.print(table)

    def print_summary(
        self,
        console: Optional[RichConsole] = None,
        total_program_target: Optional[int] = None,
    ) -> None:
        """Print a summary of the database contents to the terminal."""
        if not self.cursor or not self.conn:
            logger.error("Database not connected. Cannot print summary.")
            return

        _console = console or self.default_console or RichConsole()

        # Calculate total cost, scores, etc. from metadata of all programs
        total_api_cost = 0
        total_embed_cost = 0
        total_novelty_cost = 0
        total_meta_cost = 0
        total_compute_time = 0
        avg_score = 0.0
        best_score = 0.0  # Initialize best_score
        num_with_scores = 0
        all_scores = []
        if self.cursor:  # Ensure cursor is not None
            query = "SELECT metadata, combined_score, correct FROM programs"
            self.cursor.execute(query)
            for row in self.cursor.fetchall():
                if row["metadata"]:
                    metadata = json.loads(row["metadata"] or "{}")
                    if "api_costs" in metadata:
                        total_api_cost += float(metadata["api_costs"])
                    if "embed_cost" in metadata:
                        total_embed_cost += float(metadata["embed_cost"])
                    if "novelty_cost" in metadata:
                        total_novelty_cost += float(metadata["novelty_cost"])
                    if "meta_cost" in metadata:
                        total_meta_cost += float(metadata["meta_cost"])
                    if "compute_time" in metadata:
                        total_compute_time += float(metadata["compute_time"])
                    elif "pipeline_seconds" in metadata:
                        total_compute_time += float(metadata["pipeline_seconds"])

                if row["combined_score"] is not None:
                    score = float(row["combined_score"])
                    avg_score += score
                    # Only update best_score for correct programs
                    if row["correct"] and score > best_score:
                        best_score = score
                    num_with_scores += 1
                    all_scores.append(score)
        # Table 1: Summary Table
        summary_table = RichTable(
            title="[bold cyan]Program Database Summary[/bold cyan]",
            border_style="cyan",
            box=rich.box.ROUNDED,
            width=40,  # Set width for consistent layout
        )
        summary_table.add_column("Metric", style="cyan bold", no_wrap=True)
        summary_table.add_column("Value", style="magenta")

        # Add Best Score to the top of the summary table
        summary_table.add_row(
            "Overall Best Score",
            f"[bold cyan]{best_score:.2f}[/bold cyan]"
            if num_with_scores > 0
            else "[dim]N/A[/dim]",
        )

        # Gather data for summary
        total_programs = self.count_programs_func()
        total_programs_display = f"[bold]{total_programs}[/bold]"
        if total_program_target is not None:
            total_programs_display = (
                f"[bold]{total_programs}[/bold] / {total_program_target}"
            )
        summary_table.add_row("Total Programs", total_programs_display)

        # Correctness info
        self.cursor.execute("SELECT COUNT(*) FROM programs WHERE correct = 1")
        correct_programs = (self.cursor.fetchone() or [0])[0]
        correct_percentage = (
            (correct_programs / total_programs * 100) if total_programs > 0 else 0
        )
        summary_table.add_row(
            "Correct Programs",
            f"[bold]{correct_programs}[/bold] / {total_programs} "
            f"({correct_percentage:.0f}%)",
        )

        # Archive info
        self.cursor.execute("SELECT COUNT(*) FROM archive")
        archive_count = (self.cursor.fetchone() or [0])[0]
        archive_percentage = (
            (archive_count / self.config.archive_size * 100)
            if self.config.archive_size > 0
            else 0
        )
        summary_table.add_row(
            "Archived Programs",
            f"[bold]{archive_count}[/bold] / {self.config.archive_size} "
            f"({archive_percentage:.0f}%)",
        )

        # Island populations
        if hasattr(self.config, "num_islands") and self.config.num_islands > 0:
            island_str = self.island_manager.format_island_display()
            summary_table.add_row("Island Populations", island_str)

            # Display migration info
            migration_info = self.island_manager.get_migration_info()
            if migration_info:
                summary_table.add_row("Migration Policy", migration_info)

        # Table 2: Best Program Details
        best_program_table_renderable = None
        best_program = self.get_best_program_func()
        if best_program:
            best_program_table = RichTable(
                title="[bold yellow]Best Program[/bold yellow]",
                border_style="yellow",
                box=rich.box.ROUNDED,
                width=40,  # Set width for consistent layout
            )
            best_program_table.add_column("Attribute", style="green bold")
            best_program_table.add_column("Value", style="yellow")

            # Short ID (for reference)
            short_id = (
                best_program.id[:8] + "..."
                if len(best_program.id) > 8
                else best_program.id
            )
            best_program_table.add_row("ID", f"[dim]{short_id}[/dim]")
            best_program_table.add_row("Generation", str(best_program.generation))
            complexity_val = f"{best_program.complexity:.2f}"
            best_program_table.add_row("Complexity", complexity_val)
            if best_program.embedding:
                embedding_val = f"{best_program.embedding[0]:.2f}"
            else:
                embedding_val = "N/A"
            best_program_table.add_row("Embedding[0]", embedding_val)

            if best_program.combined_score is not None:
                score_display = (
                    f"[bold green]{best_program.combined_score:.2f}[/bold green]"
                )
                best_program_table.add_row("Metric: Score", score_display)
            else:
                best_program_table.add_row("Metrics", "[dim]N/A[/dim]")

            # Format timestamp nicely
            timestamp_display = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(best_program.timestamp)
            )
            best_program_table.add_row("Timestamp", timestamp_display)
            best_program_table_renderable = best_program_table

        # Table 3: Cost & Stats Summary
        cost_table = RichTable(
            title="[bold magenta]Cost & Stats Summary[/bold magenta]",
            border_style="magenta",
            box=rich.box.ROUNDED,
            width=40,  # Set width for consistent layout
        )
        cost_table.add_column("Metric", style="magenta bold")
        cost_table.add_column("Value", style="green")

        # Add cost information
        total_cost = (
            total_api_cost + total_embed_cost + total_novelty_cost + total_meta_cost
        )
        if total_cost > 0:
            cost_table.add_row("Total API Cost", f"[bold]${total_api_cost:.2f}[/bold]")
            cost_table.add_row(
                "Total Embedding Cost", f"[bold]${total_embed_cost:.2f}[/bold]"
            )
            cost_table.add_row(
                "Total Novelty Cost", f"[bold]${total_novelty_cost:.2f}[/bold]"
            )
            cost_table.add_row(
                "Total Meta Cost", f"[bold]${total_meta_cost:.2f}[/bold]"
            )
            cost_table.add_row("Total Combined Cost", f"[bold]${total_cost:.2f}[/bold]")

        # Add compute time if available
        if total_compute_time > 0:
            hours = int(total_compute_time // 3600)
            minutes = int((total_compute_time % 3600) // 60)
            seconds = int(total_compute_time % 60)
            time_str = f"{hours}h {minutes}m {seconds}s"
            cost_table.add_row("Total Compute", time_str)

        # Print Summary, Best Program, and Cost tables (side-by-side)
        tables_to_display = [summary_table]
        if best_program_table_renderable:
            tables_to_display.append(best_program_table_renderable)
        tables_to_display.append(cost_table)

        _console.print(RichColumns(tables_to_display))

        # Table 4: Top Performing Programs (Modified to show best performers)
        highlight_table = RichTable(
            title="[bold green]Top 10 Best Performing Programs[/bold green]",
            border_style="green",
            box=rich.box.ROUNDED,
            show_lines=True,
            width=120,  # Match program summary table width
        )
        # Columns designed to fit within 120 width
        highlight_table.add_column(
            "Rank", style="magenta bold", justify="center", width=6
        )
        highlight_table.add_column("Gen", style="cyan bold", justify="center", width=6)
        highlight_table.add_column("✓/✗", style="red bold", justify="center", width=4)
        highlight_table.add_column(
            "Score", style="green bold", justify="right", width=8
        )
        highlight_table.add_column(
            "Complexity", style="yellow", justify="right", width=10
        )
        highlight_table.add_column(
            "Patch Name",
            style="blue",
            justify="left",
            width=32,
            overflow="ellipsis",
        )
        highlight_table.add_column("Type", style="cyan", justify="left", width=8)
        highlight_table.add_column("Island", style="magenta", justify="center", width=8)
        highlight_table.add_column("Children", style="blue", justify="right", width=8)
        highlight_table.add_column("Timestamp", style="dim", width=19)

        # Fetch top performing programs ordered by combined_score
        query = (
            "SELECT id, code, language, parent_id, generation, timestamp, "
            "combined_score, public_metrics, private_metrics, "
            "complexity, embedding, metadata, correct, island_idx, "
            "children_count "
            "FROM programs WHERE combined_score IS NOT NULL AND correct = 1 "
            "ORDER BY combined_score DESC LIMIT 10"
        )
        self.cursor.execute(query)
        top_program_rows = self.cursor.fetchall()

        if not top_program_rows:
            msg = "[yellow]No programs with scores in the database to display.[/yellow]"
            _console.print(msg)
            return

        # Process top performing programs
        for rank, row_data in enumerate(top_program_rows, 1):
            p_dict = dict(row_data)
            p_dict["public_metrics"] = (
                json.loads(p_dict["public_metrics"])
                if p_dict.get("public_metrics")
                else {}
            )
            p_dict["private_metrics"] = (
                json.loads(p_dict["private_metrics"])
                if p_dict.get("private_metrics")
                else {}
            )
            metadata_str = p_dict.get("metadata")
            p_dict["metadata"] = json.loads(metadata_str) if metadata_str else {}
            # Make sure 'correct' is properly converted to boolean
            if "correct" in p_dict:
                p_dict["correct"] = bool(p_dict["correct"])

            # Import Program class here to avoid circular imports
            from .dbase import Program

            prog = Program.from_dict(p_dict)

            # Format values
            combined_score_val = prog.combined_score
            combined_score_str = (
                f"{combined_score_val:.3f}" if combined_score_val is not None else "N/A"
            )

            # Correctness indicator
            correct_str = (
                "[bold green]✓[/bold green]"
                if prog.correct
                else "[bold red]✗[/bold red]"
            )

            # Island display
            island_display = (
                f"I{prog.island_idx}" if prog.island_idx is not None else "N/A"
            )

            # Children count
            children_count = prog.children_count or 0

            timestamp = time.localtime(prog.timestamp)
            ts_str = time.strftime("%Y-%m-%d %H:%M:%S", timestamp)

            # Rank styling - top 3 get special colors
            if rank == 1:
                rank_str = "[bold gold1]#1[/bold gold1]"
                score_str = f"[bold gold1]{combined_score_str}[/bold gold1]"
            elif rank == 2:
                rank_str = "[bold bright_white]#2[/bold bright_white]"
                score_str = (
                    f"[bold bright_white]{combined_score_str}[/bold bright_white]"
                )
            elif rank == 3:
                rank_str = "[bold orange1]#3[/bold orange1]"
                score_str = f"[bold orange1]{combined_score_str}[/bold orange1]"
            else:
                rank_str = f"#{rank}"
                score_str = combined_score_str

            highlight_table.add_row(
                rank_str,
                str(prog.generation),
                correct_str,
                score_str,
                f"{prog.complexity:.1f}",
                prog.metadata.get("patch_name", "N/A")[:30],
                prog.metadata.get("patch_type", "N/A")[:6],
                island_display,
                str(children_count),
                ts_str,
            )

        _console.print(highlight_table)

    def set_last_iteration(self, last_iteration: int):
        """Set the last iteration for display purposes."""
        self.last_iteration = last_iteration

    def print_sampling_summary(
        self,
        parent,
        archive_inspirations,
        top_k_inspirations,
        target_generation=None,
        novelty_attempt=None,
        max_novelty_attempts=None,
        resample_attempt=None,
        max_resample_attempts=None,
        ancestor_inspirations=None,
        is_fix_mode=False,
        console: Optional[RichConsole] = None,
    ):
        """Print a summary of the sampled parent and inspirations."""
        _console = console or self.default_console or RichConsole()

        # Determine generation to display - use provided target_generation or fallback
        if target_generation is not None:
            gen_display = target_generation
        else:
            # Fallback to parent.generation + 1 (may not be accurate)
            gen_display = parent.generation + 1

        # Calculate total API costs
        total_api_cost = 0
        if self.cursor:
            query = "SELECT metadata FROM programs"
            self.cursor.execute(query)
            for row in self.cursor.fetchall():
                if row["metadata"]:
                    metadata = json.loads(row["metadata"] or "{}")
                    if "api_costs" in metadata:
                        total_api_cost += float(metadata["api_costs"])
                    if "embed_cost" in metadata:
                        total_api_cost += float(metadata["embed_cost"])
                    if "novelty_cost" in metadata:
                        total_api_cost += float(metadata["novelty_cost"])
                    if "meta_cost" in metadata:
                        total_api_cost += float(metadata["meta_cost"])

        # Create title with generation and attempt information
        title_parts = [
            f"[bold red]Parent & Context Sampling Summary - "
            f"Gen {gen_display} | Total Cost: ${total_api_cost:.2f}"
        ]

        # Add attempt information if provided
        if (
            novelty_attempt is not None
            and max_novelty_attempts is not None
            and resample_attempt is not None
            and max_resample_attempts is not None
        ):
            title_parts.append(
                f" (Novelty: {novelty_attempt}/{max_novelty_attempts}, "
                f"Resample: {resample_attempt}/{max_resample_attempts})"
            )

        title_parts.append("[/bold red]")

        # Create main sampling summary table with same width as program summary
        # Include generation and attempt info in title for better tracking
        table = RichTable(
            title="".join(title_parts),
            border_style="red",
            show_header=True,
            header_style="bold cyan",
            width=120,  # Match program summary table width
        )

        # Add columns matching program summary structure
        table.add_column("Role", style="cyan bold", width=12)
        table.add_column("Gen", style="magenta", justify="center", width=5)
        table.add_column("Island", style="red", justify="center", width=8)
        table.add_column("✓/✗", style="white", justify="center", width=6)
        table.add_column("Score", style="green", justify="right", width=8)
        table.add_column(
            "Patch Name",
            style="yellow",
            justify="left",
            width=32,
            overflow="ellipsis",
        )
        table.add_column(
            "Type",
            style="yellow",
            justify="left",
            width=6,
            overflow="ellipsis",
        )
        table.add_column("Complex", style="blue", justify="right", width=7)
        table.add_column("Cost", style="green", justify="right", width=7)
        table.add_column("Time", style="cyan", justify="right", width=5)

        def format_program_row(prog, role_name):
            """Format a program row with all details."""
            # Score with color coding
            if prog.combined_score is not None:
                score = prog.combined_score
                if score > 0.8:
                    score_display = f"[bold green]{score:.3f}[/bold green]"
                elif score > 0.5:
                    score_display = f"[green]{score:.3f}[/green]"
                else:
                    score_display = f"[yellow]{score:.3f}[/yellow]"
            else:
                score_display = "[dim]N/A[/dim]"

            # Island
            island = f"I-{prog.island_idx}" if prog.island_idx is not None else "N/A"

            # Correctness
            correct = (
                "[bold green]✓[/bold green]"
                if prog.correct
                else "[bold red]✗[/bold red]"
            )

            # Cost breakdown
            cost_display = "[dim]N/A[/dim]"
            if prog.metadata:
                api_cost = float(prog.metadata.get("api_costs", 0))
                embed_cost = float(prog.metadata.get("embed_cost", 0))
                novelty_cost = float(prog.metadata.get("novelty_cost", 0))
                meta_cost = float(prog.metadata.get("meta_cost", 0))
                total_cost = api_cost + embed_cost + novelty_cost + meta_cost
                cost_display = f"${total_cost:.3f}"

            # Time
            time_display = "[dim]N/A[/dim]"
            time_val = None
            if prog.metadata:
                time_val = prog.metadata.get(
                    "compute_time", prog.metadata.get("pipeline_seconds")
                )
            if time_val is not None:
                if time_val > 60:
                    time_display = f"{time_val / 60:.1f}m"
                else:
                    time_display = f"{time_val:.1f}s"

            # Patch name and type
            patch_name = prog.metadata.get("patch_name", "[dim]N/A[/dim]")[:30]
            patch_type = prog.metadata.get("patch_type", "[dim]N/A[/dim]")

            return [
                role_name,
                str(prog.generation),
                island,
                correct,
                score_display,
                patch_name,
                patch_type,
                f"{prog.complexity:.1f}",
                cost_display,
                time_display,
            ]

        # Add parent row (labeled as FIX TARGET in fix mode)
        parent_label = (
            "[bold red]FIX TARGET[/bold red]" if is_fix_mode else "[bold]PARENT[/bold]"
        )
        table.add_row(*format_program_row(parent, parent_label))

        # Add ancestor inspirations (for fix mode)
        if ancestor_inspirations:
            for i, prog in enumerate(ancestor_inspirations):
                table.add_row(*format_program_row(prog, f"ANCESTOR-{i + 1}"))

        # Add archive inspirations
        for i, prog in enumerate(archive_inspirations):
            table.add_row(*format_program_row(prog, f"Archive-{i + 1}"))

        # Add top-k inspirations
        for i, prog in enumerate(top_k_inspirations):
            table.add_row(*format_program_row(prog, f"TopK-{i + 1}"))

        _console.print(table)
