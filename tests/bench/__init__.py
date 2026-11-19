"""A benchmark of real policies over generated data (proposal 0020).

``rebac.skeleton.export`` writes a project's effective policy and the skeleton
of the models it touches. ``tests/bench/policies/<name>/`` holds such an
export; ``tests.benchapp`` rebuilds stand-in models from it, ``data`` fills
them to a chosen size, and ``scenarios`` reads them as an application would.
Nothing here runs in the default test tiers: see ``tests/bench/README.md``.
"""
