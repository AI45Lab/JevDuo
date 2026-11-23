"""Label the decision observation and any completed post-action observation."""


def turn_event(turn, before, after=None):
    event = {"turn": turn, "screen": before, "screen_phase": "before_action"}
    if after is not None:
        event.update(after_screen=after, after_screen_phase="after_action")
    return event
