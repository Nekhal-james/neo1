cmake_minimum_required(VERSION 3.8)
project(neo_bringup)

find_package(ament_cmake REQUIRED)

install(DIRECTORY launch config
  DESTINATION share/${PROJECT_NAME}
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()

  # Plain pytest over the YAML -- no ROS needed, so the same test also runs in
  # the pip-only CI job.
  find_package(ament_cmake_pytest REQUIRED)
  ament_add_pytest_test(profiles test/test_profiles.py)
endif()

ament_package()
