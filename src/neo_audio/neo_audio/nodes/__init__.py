"""ROS bindings for the audio path.

Each module here is a thin wrapper: it converts messages and calls into the
already-tested core beside it. Same discipline as neo_motion/nodes and
neo_perception/nodes -- logic that is worth testing does not live in a node.
"""
