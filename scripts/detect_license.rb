# frozen_string_literal: true

require "json"
require "licensee"

abort "usage: detect_license.rb PATH" unless ARGV.length == 1

Licensee.confidence_threshold = 98
project = Licensee.project(
  ARGV.fetch(0),
  detect_packages: false,
  detect_readme: false,
  filesystem: true
)
matched_files = project.matched_files.select { |matched_file| matched_file.directory == "." }
licenses = matched_files.map(&:license).compact.uniq

puts JSON.generate(
  licenses: licenses.map { |license| { spdx_id: license.spdx_id } },
  matched_files: matched_files.map do |matched_file|
    { matched_license: matched_file.matched_license }
  end
)
