import hashlib
import unittest
from scripts import render_challenge as r
from scripts.render_pagination import retain_entrypoint_navigation

class RuntimeIdentity(unittest.TestCase):
    def test_both_official_runtime_bytes_unchanged(self):
        self.assertEqual(hashlib.sha256(r.RUNTIME_SANITIZER.encode()).hexdigest(),"37003e46e52ea8ca4ab4dc59e99a5fa35c4db8e029abddd7c4996df8b94815f6")
        self.assertEqual(hashlib.sha256(r.VERSO_RUNTIME.encode()).hexdigest(),"332773edafa3ac712ec3fba31d4c6ece2339693958873a9020a9be1bddb22538")

    def test_output_caps_unchanged(self):
        self.assertEqual((r.MAX_RENDER_FILE_BYTES,r.MAX_RENDER_BYTES,r.MAX_RENDER_FILES,r.MAX_RENDER_NODES),(8388608,26214400,2000,4000))

    def test_existing_docstring_is_preserved_with_navigation(self):
        source='<section class="code-content"><div class="md-text"><p>Original math &amp; prose.</p></div><code class="hl lean block"><span id="a" data-binding="const-A">proof</span></code></section>'
        expected=source.replace('</p></div>','</p><nav>links</nav></div>')
        self.assertEqual(retain_entrypoint_navigation(source,'<nav>links</nav>','A'),expected)

    def test_missing_docstring_adds_link_only_container(self):
        source='<section class="code-content"><code class="hl lean block"><span id="a" data-binding="const-A">proof</span></code></section>'
        self.assertEqual(retain_entrypoint_navigation(source,'<nav>links</nav>','A'),source.replace('<code','<div class="md-text"><nav>links</nav></div><code'))

    def test_duplicate_navigation_target_rejected(self):
        block='<code class="hl lean block"><span id="a" data-binding="const-A">proof</span></code>'
        source='<section class="code-content">'+block+block+'</section>'
        with self.assertRaisesRegex(ValueError,'duplicated'):
            retain_entrypoint_navigation(source,'<nav>links</nav>','A')

    def test_malformed_navigation_target_rejected(self):
        source='<section class="code-content"><code class="hl lean block"><span id="a" data-binding="const-A">proof</span></code>'
        with self.assertRaisesRegex(ValueError,'Malformed'):
            retain_entrypoint_navigation(source,'<nav>links</nav>','A')
