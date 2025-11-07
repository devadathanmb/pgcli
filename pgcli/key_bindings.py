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
    vi_insert_mode,
    vi_navigation_mode,
)

from .pgbuffer import buffer_should_be_handled, safe_multi_line_mode

_logger = logging.getLogger(__name__)


def setup_vim_cursor_shapes():
    """
    Configure cursor shape changes for vim modes.

    Uses terminal escape sequences to change cursor appearance:
    - Block cursor (█) in navigation/normal mode
    - Beam cursor (|) in insert mode
    - Underline cursor (_) in replace mode
    """
    def set_input_mode(self, mode):
        # Cursor shape codes: 1=block, 3=underline, 5=beam
        shape = {
            InputMode.NAVIGATION: 1,  # Block cursor for normal mode
            InputMode.REPLACE: 3,      # Underline cursor for replace mode
            InputMode.INSERT: 5,       # Beam cursor for insert mode
        }.get(mode, 5)

        # Send escape sequence to terminal
        out = getattr(sys.stdout, 'buffer', sys.stdout)
        try:
            out.write('\x1b[{} q'.format(shape).encode('ascii'))
            sys.stdout.flush()
        except (AttributeError, OSError):
            # Silently ignore if terminal doesn't support cursor shape changes
            pass

        self._input_mode = mode

    # Patch ViState to include cursor shape changes
    ViState._input_mode = InputMode.INSERT
    ViState.input_mode = property(lambda self: self._input_mode, set_input_mode)


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

        # Setup cursor shapes when switching to vim mode
        if pgcli.vi_mode:
            setup_vim_cursor_shapes()
        else:
            # Reset to default beam cursor when switching to emacs mode
            out = getattr(sys.stdout, 'buffer', sys.stdout)
            try:
                out.write(b'\x1b[5 q')  # Beam cursor
                sys.stdout.flush()
            except (AttributeError, OSError):
                pass

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

    @kb.add("escape", filter=has_completions)
    def _(event):
        """Force closing of autocompletion."""
        _logger.debug("Detected <Esc> key.")

        event.current_buffer.complete_state = None
        event.app.current_buffer.complete_state = None

    @kb.add("c-space")
    def _(event):
        """
        Toggle autocompletion at cursor.

        If the autocompletion menu is not showing, display it with the
        appropriate completions for the context.

        If the menu is showing, close it (toggle off).
        """
        _logger.debug("Detected <C-Space> key.")

        b = event.app.current_buffer
        if b.complete_state:
            # Close completion menu (toggle off)
            b.complete_state = None
        else:
            # Open completion menu (toggle on)
            b.start_completion(select_first=False)

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
        """Check if there's a suggestion and cursor is at end of line."""
        from prompt_toolkit.application.current import get_app
        app = get_app()
        buffer = app.current_buffer
        return (
            buffer.suggestion is not None
            and buffer.document.is_cursor_at_the_end_of_line
        )

    @kb.add("l", filter=vi_navigation_mode & has_suggestion_at_end, eager=True)
    def _(event):
        """
        Accept autosuggestion with 'l' in vi normal mode when at end of line.

        This takes precedence over normal 'l' movement when a suggestion is available.
        """
        _logger.debug("Accepting suggestion with 'l' in normal mode")
        buff = event.current_buffer
        suggestion = buff.suggestion
        if suggestion:
            buff.insert_text(suggestion.text)

    @kb.add("l", filter=vi_navigation_mode, eager=False)
    def _(event):
        """Normal 'l' forward movement when no suggestion or not at end."""
        buff = event.current_buffer
        buff.cursor_position += buff.document.get_cursor_right_position()

    @kb.add("right", filter=vi_navigation_mode & has_suggestion_at_end, eager=True)
    def _(event):
        """Accept autosuggestion with right arrow in vi normal mode when at end of line."""
        _logger.debug("Accepting suggestion with right arrow in normal mode")
        buff = event.current_buffer
        suggestion = buff.suggestion
        if suggestion:
            buff.insert_text(suggestion.text)

    @kb.add("right", filter=vi_navigation_mode, eager=False)
    def _(event):
        """Normal right arrow forward movement when no suggestion or not at end."""
        buff = event.current_buffer
        buff.cursor_position += buff.document.get_cursor_right_position()

    return kb
