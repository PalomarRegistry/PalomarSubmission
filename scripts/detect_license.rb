# frozen_string_literal: true

require "json"
require "licensee"

abort "usage: detect_license.rb PATH" unless ARGV.length == 1

# Licensee only knows the choosealicense.com corpus, which omits the Creative
# Commons NonCommercial licences that Palomar accepts. Palomar adds their SPDX
# texts from licence-supplement/ so that the same 98% similarity test detects
# them. See licence-supplement/README.md for provenance.
module PalomarLicenceSupplement
  ROOT = File.expand_path("../licence-supplement", __dir__)
  LICENSE_DIR = File.join(ROOT, "_licenses")
  SPDX_DIR = File.join(ROOT, "license-list-XML")

  def self.keys
    @keys ||= Dir.glob(File.join(LICENSE_DIR, "*.txt")).map { |path| File.basename(path, ".txt") }
  end

  def self.include?(license)
    keys.include?(license.key)
  end

  def self.noncommercial?(license)
    include?(license) && license.key.start_with?("cc-by-nc-")
  end

  # Adds the supplementary licence files to licensee's corpus.
  module ClassMethods
    def license_files
      @license_files ||= super + Dir.glob(File.join(LICENSE_DIR, "*.txt"))
    end
  end

  # Resolves supplementary licences to their vendored text and SPDX XML.
  module InstanceMethods
    def path
      return super unless PalomarLicenceSupplement.include?(self)

      @path ||= File.join(LICENSE_DIR, "#{key}.txt")
    end

    private

    def spdx_alt_segments
      return super unless PalomarLicenceSupplement.include?(self)

      @spdx_alt_segments ||= begin
        raw_xml = File.read(File.join(SPDX_DIR, "#{spdx_id}.xml"), encoding: "utf-8")
        text = raw_xml.match(%r{<text>(.*)</text>}m)[1]
        text.gsub!(%r{<copyrightText>.*?</copyrightText>}m, "")
        text.gsub!(%r{<titleText>.*?</titleText>}m, "")
        text.gsub!(%r{<optional.*?>.*?</optional>}m, "")
        text.scan(/<alt .*?>/m).size
      end
    end
  end

  # Licensee refuses to compare any CC licence with a file whose title names a
  # NonCommercial or NoDerivatives variant, so that such a file is never reported
  # as CC-BY or CC-BY-SA. The supplementary NonCommercial licences are exactly
  # the licences those files should be compared with, so they are exempt.
  module DiceMethods
    def potential_matches
      @potential_matches ||= begin
        upstream = super
        supplement = Licensee.licenses(hidden: true, pseudo: false).select do |license|
          PalomarLicenceSupplement.noncommercial?(license) && license.wordset
        end
        (upstream + supplement).uniq
      end
    end
  end
end

Licensee::License.singleton_class.prepend(PalomarLicenceSupplement::ClassMethods)
Licensee::License.prepend(PalomarLicenceSupplement::InstanceMethods)
Licensee::Matchers::Dice.prepend(PalomarLicenceSupplement::DiceMethods)

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
