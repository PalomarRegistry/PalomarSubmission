"""Synthetic HTML/manifest controls, not Lean proofs or canonical acceptance."""
from pathlib import Path
import json
import tempfile
import unittest
from scripts import render_pagination as r

class Pagination(unittest.TestCase):
    def fixture(self, directory):
        root=Path(directory)/'raw'; module=root/'HodgeChallenge'; module.mkdir(parents=True)
        metadata={'schema_version':1,'module':'HodgeChallenge','pages':['source-1.html'],
                  'source_commands':1,'selected_declarations':['A']}
        (module/'source-pages.json').write_text(json.dumps(metadata))
        command='<code class="hl lean block"><span id="a" class="const" data-binding="const-A">example proof &lt;x&gt;</span></code>'
        document='<html><head><base href="../"/></head><body><section class="code-content">{}</section></body></html>'
        navigation='<nav aria-label="Complete source"><a href="HodgeChallenge/source-1.html">Source page 1</a></nav>'
        (module/'source-1.html').write_text(document.format(navigation+command))
        (module/'index.html').write_text(document.format(command))
        return root,module,metadata,command

    def test_positive_full_command_bytes_and_navigation_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root,module,metadata,command=self.fixture(directory)
            out=Path(directory)/'routed'
            result=r.prepare(root,out,'HodgeChallenge',expected_selected=['A'])
            for name in ['source-1.html','index.html']:
                text=(out/'HodgeChallenge'/name).read_text()
                self.assertIn(command,text)
                self.assertEqual(text.count('class="palomar-source-navigation"'),1)
                self.assertIn('Challenge/index.html',text)
            self.assertIn('<div class="md-text"><nav',(out/'HodgeChallenge/index.html').read_text())
            self.assertEqual(result['source_pages'],1)

    def test_file_cap_precedes_manifest_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'raw'; root.mkdir()
            with (root/'oversized').open('wb') as stream: stream.truncate(8388609)
            with self.assertRaisesRegex(ValueError,'File cap'):
                r.prepare(root,Path(directory)/'out','HodgeChallenge')

    def test_total_cap_precedes_manifest_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'raw'; root.mkdir()
            for index in range(4):
                with (root/str(index)).open('wb') as stream: stream.truncate(7*1024*1024)
            with self.assertRaisesRegex(ValueError,'Artifact cap'):
                r.prepare(root,Path(directory)/'out','HodgeChallenge')

    def test_file_count_precedes_manifest_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'raw'; root.mkdir()
            for index in range(2001): (root/str(index)).touch()
            with self.assertRaisesRegex(ValueError,'Artifact cap'):
                r.prepare(root,Path(directory)/'out','HodgeChallenge')

    def test_node_cap_precedes_manifest_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'raw'; root.mkdir()
            for index in range(4001): (root/str(index)).mkdir()
            with self.assertRaisesRegex(ValueError,'Node cap'):
                r.prepare(root,Path(directory)/'out','HodgeChallenge')

    def test_nested_output_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root,module,metadata,command=self.fixture(directory)
            with self.assertRaisesRegex(ValueError,'outside'):
                r.prepare(root,root/'out','HodgeChallenge')

    def test_symlink_input_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root,module,metadata,command=self.fixture(directory)
            alias=Path(directory)/'alias'; alias.symlink_to(root)
            with self.assertRaisesRegex(ValueError,'real input'):
                r.prepare(alias,Path(directory)/'out','HodgeChallenge')

    def test_symlink_page_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root,module,metadata,command=self.fixture(directory)
            (module/'source-1.html').unlink()
            (module/'source-1.html').symlink_to(module/'index.html')
            with self.assertRaisesRegex(ValueError,'Symlink'):
                r.prepare(root,Path(directory)/'out','HodgeChallenge')

    def test_manifest_selection_bound_to_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root,module,metadata,command=self.fixture(directory)
            with self.assertRaisesRegex(ValueError,'audited comparator'):
                r.prepare(root,Path(directory)/'out','HodgeChallenge',expected_selected=['B'])

    def test_unlisted_source_page_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root,module,metadata,command=self.fixture(directory)
            (module/'source-2.html').write_text('unexpected')
            with self.assertRaisesRegex(ValueError,'inventory'):
                r.prepare(root,Path(directory)/'out','HodgeChallenge')

    def test_duplicate_manifest_page_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root,module,metadata,command=self.fixture(directory)
            metadata['pages']*=2
            (module/'source-pages.json').write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError,'duplicate'):
                r.prepare(root,Path(directory)/'out','HodgeChallenge')

    def test_duplicate_anchor_within_page_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root,module,metadata,command=self.fixture(directory)
            page=module/'source-1.html'
            page.write_text(page.read_text().replace(command,command+command))
            with self.assertRaisesRegex(ValueError,'Duplicate source anchor'):
                r.prepare(root,Path(directory)/'out','HodgeChallenge')

    def test_duplicate_anchor_across_pages_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root,module,metadata,command=self.fixture(directory)
            metadata['pages'].append('source-2.html')
            (module/'source-pages.json').write_text(json.dumps(metadata))
            (module/'source-2.html').write_text((module/'source-1.html').read_text())
            with self.assertRaisesRegex(ValueError,'Duplicate source anchor'):
                r.prepare(root,Path(directory)/'out','HodgeChallenge')

    def test_json_routing_collision_rejected(self):
        owners={'a':'HodgeChallenge/source-1.html'}
        with self.assertRaisesRegex(ValueError,'collides'):
            r.route_json({'HodgeChallenge/#a':1,'HodgeChallenge/source-1.html#a':2},'HodgeChallenge',owners)
