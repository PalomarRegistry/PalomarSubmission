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

  # Licensee strips any leading line that starts with a known licence title
  # before comparing texts. Supplementary titles are kept out of that pattern,
  # so that adding them cannot hide a line such as "CC-BY-NC-4.0; no commercial
  # use" at the top of an otherwise standard MIT text. Supplementary texts are
  # matched with their titles in place instead.
  module TitleMethods
    def title_regex
      @title_regex ||= begin
        licenses = Licensee::License.all(hidden: true, pseudo: false).reject do |license|
          PalomarLicenceSupplement.include?(license)
        end
        titles = licenses.map(&:title_regex)
        without_versions = licenses.map do |license|
          next if license.title == license.name_without_version

          Regexp.new Regexp.escape(license.name_without_version), "i"
        end
        titles.concat(without_versions.compact)
        /#{Licensee::ContentHelper::START_REGEX}\(?(?:the )?#{Regexp.union titles}.*?$/i
      end
    end
  end

  # A file that names a NoDerivatives licence anywhere, by title, SPDX-style
  # identifier, or legal-code URL, is never accepted, whatever it matches.
  # Otherwise a NonCommercial-NoDerivatives text edited to within the similarity
  # threshold of a NonCommercial licence would be reported as that licence.
  NO_DERIVATIVES = %r{
    attribution[\s-]*(?:non[\s-]*commercial[\s-]*)?no[\s-]*deriv
    | licen[cs]es/by-(?:nc-)?nd\b
    | \bcc[\s-]+by[\s-]+(?:nc[\s-]+)?nd\b
  }xi

  def self.names_no_derivatives?(path)
    File.read(path, encoding: "utf-8").match?(NO_DERIVATIVES)
  end
end

Licensee::License.singleton_class.prepend(PalomarLicenceSupplement::ClassMethods)
Licensee::ContentHelper.singleton_class.prepend(PalomarLicenceSupplement::TitleMethods)
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
matched_files = [] if PalomarLicenceSupplement.names_no_derivatives?(ARGV.fetch(0))
licenses = matched_files.map(&:license).compact.uniq

puts JSON.generate(
  licenses: licenses.map { |license| { spdx_id: license.spdx_id } },
  matched_files: matched_files.map do |matched_file|
    { matched_license: matched_file.matched_license }
  end
)
