"""A fixed capacity ceiling. For exercising the shed path without a signal."""


class StaticCeiling:
    """The same answer, always.

    A real ceiling for a hard budget -- a circuit that cannot carry more than N
    nodes -- and the way to watch a shed happen in dry_run or on a bench
    without waiting for a heat wave. 0 is a ceiling (shed every node), not an
    absent one, which is why this takes an int and refuses everything else.
    """

    def __init__(self, nodes):
        if isinstance(nodes, bool) or not isinstance(nodes, int) or nodes < 0:
            raise ValueError("a static ceiling is a whole number of nodes, "
                             "0 or more; got %r" % (nodes,))
        self.nodes = nodes

    def limit(self):
        return self.nodes
