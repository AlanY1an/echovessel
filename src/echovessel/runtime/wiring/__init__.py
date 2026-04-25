"""DI wiring — composition root for memory / importer / prompts / observers.

The runtime owns dependency injection between layers. This sub-package
holds the factory functions, mediators, and Protocol implementations
that connect upper-layer Protocols to lower-layer implementations at
startup. Files land here in commit C3.
"""
