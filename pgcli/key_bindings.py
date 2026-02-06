import logging
import sys
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.vi_state import InputMode, ViState
from prompt_toolkit.filters import (
    completion_is_selected,
    is_searching,
    has_completions,
    has_selection,
    vi_mode,
    vi_navigation_mode,
)
from prompt_toolkit.layout.processors import Processor, Transformation

from .pgbuffer import buffer_should_be_handled, safe_multi_line_mode

_logger = logging.getLogger(__name__)

# Track whether ViState has been patched to avoid redundant monkeypatching
_vim_cursor_shapes_configured = False


def _set_cursor_shape(shape_code):
    """
    Send terminal escape sequence to change cursor shape.

    Args:
        shape_code: Cursor shape code (1=block, 3=underline, 5=beam)
    """
    out = getattr(sys.stdout, 'buffer', sys.stdout)
    try:
        # Write escape sequence as bytes
        out.write(f'\x1b[{shape_code} q'.encode('ascii'))
        sys.stdout.flush()
    except (AttributeError, OSError):
        # Silently ignore if terminal doesn't support cursor shape changes
        pass


def setup_vim_cursor_shapes():
    """
    Configure cursor shape changes for vim modes (idempotent).

    Uses terminal escape sequences to change cursor appearance:
    - Block cursor (█) in navigation/normal mode
    - Beam cursor (|) in insert mode
    - Underline cursor (_) in replace mode

    This function can be called multiple times safely - it only patches ViState once.
    """
    global _vim_cursor_shapes_configured

    # Only patch ViState once to avoid issues with multiple calls
    if _vim_cursor_shapes_configured:
        return

    def set_input_mode(self, mode):
        # Cursor shape codes: 1=block, 3=underline, 5=beam
        shape = {
            InputMode.NAVIGATION: 1,  # Block cursor for normal mode
            InputMode.REPLACE: 3,  # Underline cursor for replace mode
            InputMode.INSERT: 5,  # Beam cursor for insert mode
        }.get(mode, 5)

        _set_cursor_shape(shape)
        self._input_mode = mode

    # Patch ViState to include cursor shape changes
    ViState._input_mode = InputMode.INSERT
    ViState.input_mode = property(lambda self: self._input_mode, set_input_mode)

    _vim_cursor_shapes_configured = True


class AppendAutoSuggestionInViMode(Processor):
    """
    Show auto-suggestions in Vi navigation mode.

    Standard prompt_toolkit only shows suggestions at cursor end position.
    Vi navigation mode places cursor one char left of end, hiding suggestions.
    This processor also shows suggestions when cursor is at end-of-line in Vi
    navigation mode, enabling fish-style 'l' key acceptance.
    """

    def __init__(self, style="class:auto-suggestion"):
        self.style = style

    def _should_show_suggestion(self, ti, suggestion):
        try:
            from prompt_toolkit.application.current import get_app

            app = get_app()
            is_vi_navigation = app.editing_mode == EditingMode.VI and app.vi_state.input_mode == InputMode.NAVIGATION
            if is_vi_navigation:
                doc = ti.document
                current_line = doc.current_line
                cursor_col = doc.cursor_position_col
                at_last_char_of_line = cursor_col == len(current_line) - 1 and len(current_line) > 0
                if at_last_char_of_line or doc.is_cursor_at_the_end_of_line:
                    return True
        except Exception:
            pass

        return False

    def apply_transformation(self, ti):
        if ti.lineno != ti.document.line_count - 1:
            return Transformation(fragments=ti.fragments)

        buffer = ti.buffer_control.buffer
        suggestion = buffer.suggestion
        suggestion_text = ""

        if suggestion and self._should_show_suggestion(ti, suggestion):
            suggestion_text = suggestion.text

        return Transformation(fragments=ti.fragments + [(self.style, suggestion_text)])


def pgcli_bindings(pgcli):
    """Custom key bindings for pgcli."""
    kb = KeyBindings()

    tab_insert_text = " " * 4

    @kb.add("f2")
    def _(event):
        """Enable/Disable SmartCompletion Mode."""
        _logger.debug("Detected F2 key.")
        pgcli.completer.smart_completion = not pgcli.completer.smart_completion

    @kb.add("f3")
    def _(event):
        """Enable/Disable Multiline Mode."""
        _logger.debug("Detected F3 key.")
        pgcli.multi_line = not pgcli.multi_line

    @kb.add("f4")
    def _(event):
        """Toggle between Vi and Emacs mode."""
        _logger.debug("Detected F4 key.")
        pgcli.vi_mode = not pgcli.vi_mode
        event.app.editing_mode = EditingMode.VI if pgcli.vi_mode else EditingMode.EMACS

        if pgcli.vi_mode:
            setup_vim_cursor_shapes()
        else:
            _set_cursor_shape(5)

    @kb.add("f5")
    def _(event):
        """Toggle between Vi and Emacs mode."""
        _logger.debug("Detected F5 key.")
        pgcli.explain_mode = not pgcli.explain_mode

    @kb.add("tab")
    def _(event):
        """Force autocompletion at cursor on non-empty lines."""

        _logger.debug("Detected <Tab> key.")

        buff = event.app.current_buffer
        doc = buff.document

        if doc.on_first_line or doc.current_line.strip():
            if buff.complete_state:
                buff.complete_next()
            else:
                buff.start_completion(select_first=True)
        else:
            buff.insert_text(tab_insert_text, fire_event=False)

    @kb.add("escape", filter=has_completions & vi_mode)
    def _(event):
        """Close autocompletion and switch to vim normal mode."""
        _logger.debug("Detected <Esc> key in vi mode with completions.")

        event.current_buffer.complete_state = None
        event.app.current_buffer.complete_state = None
        # Also switch to vim normal mode
        event.app.vi_state.input_mode = InputMode.NAVIGATION

    @kb.add("escape", filter=has_completions & ~vi_mode)
    def _(event):
        """Force closing of autocompletion in emacs mode."""
        _logger.debug("Detected <Esc> key in emacs mode with completions.")

        event.current_buffer.complete_state = None
        event.app.current_buffer.complete_state = None

    # Bind each key in toggle_completion_key to toggle completion.
    # Space-separated keys each get their own binding.
    # e.g., "c-space c-t" binds both Ctrl+Space and Ctrl+T independently.
    toggle_keys = pgcli.toggle_auto_completion_key.strip().split()

    def _toggle_completion(event):
        """
        Toggle autocompletion at cursor.

        If the autocompletion menu is not showing, display it with the
        appropriate completions for the context.

        If the menu is showing, close it (toggle off).
        """
        b = event.app.current_buffer
        if b.complete_state:
            b.complete_state = None
        else:
            b.start_completion(select_first=False)

    for _key in toggle_keys:
        kb.add(_key)(_toggle_completion)

    @kb.add("c-j", filter=has_completions)
    def _(event):
        """
        Navigate to next completion (down) in autocomplete menu.

        Works like Ctrl+n but uses Vim-style j (down) binding.
        """
        _logger.debug("Detected <C-j> key.")
        event.current_buffer.complete_next()

    @kb.add("c-k", filter=has_completions)
    def _(event):
        """
        Navigate to previous completion (up) in autocomplete menu.

        Works like Ctrl+p but uses Vim-style k (up) binding.
        """
        _logger.debug("Detected <C-k> key.")
        event.current_buffer.complete_previous()

    @kb.add("enter", filter=completion_is_selected)
    def _(event):
        """Makes the enter key work as the tab key only when showing the menu.

        In other words, don't execute query when enter is pressed in
        the completion dropdown menu, instead close the dropdown menu
        (accept current selection).

        """
        _logger.debug("Detected enter key during completion selection.")

        event.current_buffer.complete_state = None
        event.app.current_buffer.complete_state = None

    # When using multi_line input mode the buffer is not handled on Enter (a new line is
    # inserted instead), so we force the handling if we're not in a completion or
    # history search, and one of several conditions are True
    @kb.add(
        "enter",
        filter=~(completion_is_selected | is_searching) & buffer_should_be_handled(pgcli),
    )
    def _(event):
        _logger.debug("Detected enter key.")
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter", filter=~vi_mode & ~safe_multi_line_mode(pgcli))
    def _(event):
        """Introduces a line break regardless of multi-line mode or not."""
        _logger.debug("Detected alt-enter key.")
        event.app.current_buffer.insert_text("\n")

    @kb.add("c-p", filter=~has_selection)
    def _(event):
        """Move up in history."""
        event.current_buffer.history_backward(count=event.arg)

    @kb.add("c-n", filter=~has_selection)
    def _(event):
        """Move down in history."""
        event.current_buffer.history_forward(count=event.arg)

    # Add these bindings with eager=True to take precedence when suggestions are available
    # This is key for fish/zsh-style autosuggestion acceptance in vim normal mode
    from prompt_toolkit.filters import Condition

    @Condition
    def has_suggestion_at_end():
        from prompt_toolkit.application.current import get_app

        app = get_app()
        buffer = app.current_buffer
        if buffer.suggestion is None:
            return False

        doc = buffer.document
        if doc.is_cursor_at_the_end_of_line:
            return True

        current_line = doc.current_line
        cursor_col = doc.cursor_position_col
        at_last_char = cursor_col == len(current_line) - 1 and len(current_line) > 0
        return at_last_char

    @kb.add("l", filter=vi_navigation_mode & has_suggestion_at_end, eager=True)
    def _(event):
        _logger.debug("Accepting suggestion with 'l' in normal mode")
        buff = event.current_buffer
        suggestion_text = buff.suggestion.text
        buff.cursor_position = len(buff.text)
        buff.insert_text(suggestion_text, fire_event=False)

    @kb.add("right", filter=vi_navigation_mode & has_suggestion_at_end, eager=True)
    def _(event):
        _logger.debug("Accepting suggestion with right arrow in normal mode")
        buff = event.current_buffer
        suggestion_text = buff.suggestion.text
        buff.cursor_position = len(buff.text)
        buff.insert_text(suggestion_text, fire_event=False)

    return kb
