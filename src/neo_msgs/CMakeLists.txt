cmake_minimum_required(VERSION 3.8)
project(neo_msgs)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)

# Keep this list alphabetical and in sync with test/test_contract.py, which
# asserts that every interface named in docs/IMPLEMENTATION_PLAN.md section 1.2
# exists here and that nothing exists here that isn't declared.
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/AttentionTarget.msg"
  "msg/AudioChunk.msg"
  "msg/BlockCoverage.msg"
  "msg/DialogState.msg"
  "msg/EmotionState.msg"
  "msg/GestureEvent.msg"
  "msg/HeadCommand.msg"
  "msg/KbCoverage.msg"
  "msg/LinkHealth.msg"
  "msg/Reply.msg"
  "msg/SourceState.msg"
  "msg/Transcript.msg"
  "msg/WakeEvent.msg"
  "srv/QueryKb.srv"
  "srv/SetSource.srv"
  DEPENDENCIES std_msgs
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()

  # The contract test is plain pytest over the .msg/.srv text -- it needs no
  # ROS at all, which is the point: it also runs in the pip-only CI job on a
  # machine with no Jazzy install.
  find_package(ament_cmake_pytest REQUIRED)
  ament_add_pytest_test(contract test/test_contract.py)
endif()

ament_package()
