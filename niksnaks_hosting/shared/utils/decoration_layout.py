"""The title-bar button rule, kept free of GTK so it can be tested without a display.

A layout is GTK's gtk-decoration-layout string: button names separated by commas, with
a colon between the buttons on the left and the ones on the right ('appmenu:close').
"""


def with_minimize(layout: str) -> str:
    """Return *layout* with a minimize button beside close, on close's side.

    A layout that already has minimize, or has no close at all (a desktop that hides
    the title buttons on purpose), comes back unchanged.
    """
    start, colon, end = layout.partition(":")
    left, right = start.split(","), end.split(",")
    if "minimize" in left + right:
        return layout
    if "close" in right:
        i = right.index("close")
        if i and right[i - 1] == "maximize":  # keep the usual minimize,maximize,close order
            i -= 1
        right.insert(i, "minimize")
    elif "close" in left:
        left.insert(left.index("close") + 1, "minimize")
    return ",".join(left) + colon + ",".join(right)
