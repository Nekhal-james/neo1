"""Neo's audio path: the microphone, the speaker, the wake word and the router
that turns speech into text.

Everything here is importable without ROS. The nodes under `nodes/` add the
rclpy plumbing; the modules beside this one are the cores they wrap, and are
what the admin panel and the tests drive directly.
"""
