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
  # Deliberately no ament_lint_auto. It pulls in ament_lint_common, whose
  # linters enforce ROS's house style -- single quotes, import order, docstring
  # form, a copyright header on every file, an xmllint-formatted package.xml.
  # This repo has already decided against style gates: see the lint job in
  # .github/workflows/ci.yml, which runs only the bug-catching ruff rules
  # because "a lint job that fails on an unused import trains you to ignore a
  # red CI". Enabling them here contradicted that to the tune of 449 failures,
  # which nobody noticed only because colcon test was never discovering these
  # packages at all (see the --base-paths note in that workflow).

  # The contract test is plain pytest over the .msg/.srv text -- it needs no
  # ROS at all, which is the point: it also runs in the pip-only CI job on a
  # machine with no Jazzy install.
  find_package(ament_cmake_pytest REQUIRED)
  ament_add_pytest_test(contract test/test_contract.py)
endif()

ament_package()
