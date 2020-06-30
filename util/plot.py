from matplotlib.pyplot import xticks, tight_layout


def save_plot(plot, name, tilt_x_labels=False):
    """
    Save plot to a file.
    :param plot: plot object returned by df.plot(...)
    :param name: name of the file
    :param tilt_x_labels: True to tilt the x labels 45 degrees (ccw), a number to tilt them to a custom number of degrees, False not to tilt.
    """
    if tilt_x_labels:
        degrees = 45 if isinstance(tilt_x_labels, bool) else tilt_x_labels
        xticks(rotation=degrees)
    tight_layout()
    plot.get_figure().savefig(name)
