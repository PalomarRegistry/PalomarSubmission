# ruff: noqa: E501
"""Renderer-owned Verso CSS; never read styles from the submitted bundle.

Vendored from leanprover/verso f7a4dc6867f8df27778b2b320763ec9e3b7be070:
static-web/verso-vars.css, src/verso/Verso/Code/Highlighted.lean
(highlightingStyle, with Lean string escapes decoded), src/verso-html/code.css,
and vendored-js/tippy/border.css. See licenses/ for upstream license texts.

Keep these fixed and review changes when refreshing them. The declarations,
tooltips and light/dark Palomar surface use this same stylesheet at every
supported historical toolchain version. No remote fonts or CSS imports.
"""

RENDER_STYLES = r"""/*
Verso styles: Copyright (c) 2024 Lean FRO LLC. All rights reserved.
Author: David Thrane Christiansen. Apache License 2.0.
Tippy border styles: Copyright (c) 2017-present atomiks. MIT License.

Apache License 2.0 (Apache)
Apache License
Version 2.0, January 2004
http://www.apache.org/licenses/

TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

1. Definitions.

"License" shall mean the terms and conditions for use, reproduction, and distribution as defined by Sections 1 through 9 of this document.

"Licensor" shall mean the copyright owner or entity authorized by the copyright owner that is granting the License.

"Legal Entity" shall mean the union of the acting entity and all other entities that control, are controlled by, or are under common control with that entity. For the purposes of this definition, "control" means (i) the power, direct or indirect, to cause the direction or management of such entity, whether by contract or otherwise, or (ii) ownership of fifty percent (50%) or more of the outstanding shares, or (iii) beneficial ownership of such entity.

"You" (or "Your") shall mean an individual or Legal Entity exercising permissions granted by this License.

"Source" form shall mean the preferred form for making modifications, including but not limited to software source code, documentation source, and configuration files.

"Object" form shall mean any form resulting from mechanical transformation or translation of a Source form, including but not limited to compiled object code, generated documentation, and conversions to other media types.

"Work" shall mean the work of authorship, whether in Source or Object form, made available under the License, as indicated by a copyright notice that is included in or attached to the work (an example is provided in the Appendix below).

"Derivative Works" shall mean any work, whether in Source or Object form, that is based on (or derived from) the Work and for which the editorial revisions, annotations, elaborations, or other modifications represent, as a whole, an original work of authorship. For the purposes of this License, Derivative Works shall not include works that remain separable from, or merely link (or bind by name) to the interfaces of, the Work and Derivative Works thereof.

"Contribution" shall mean any work of authorship, including the original version of the Work and any modifications or additions to that Work or Derivative Works thereof, that is intentionally submitted to Licensor for inclusion in the Work by the copyright owner or by an individual or Legal Entity authorized to submit on behalf of the copyright owner. For the purposes of this definition, "submitted" means any form of electronic, verbal, or written communication sent to the Licensor or its representatives, including but not limited to communication on electronic mailing lists, source code control systems, and issue tracking systems that are managed by, or on behalf of, the Licensor for the purpose of discussing and improving the Work, but excluding communication that is conspicuously marked or otherwise designated in writing by the copyright owner as "Not a Contribution."

"Contributor" shall mean Licensor and any individual or Legal Entity on behalf of whom a Contribution has been received by Licensor and subsequently incorporated within the Work.

2. Grant of Copyright License.

Subject to the terms and conditions of this License, each Contributor hereby grants to You a perpetual, worldwide, non-exclusive, no-charge, royalty-free, irrevocable copyright license to reproduce, prepare Derivative Works of, publicly display, publicly perform, sublicense, and distribute the Work and such Derivative Works in Source or Object form.

3. Grant of Patent License.

Subject to the terms and conditions of this License, each Contributor hereby grants to You a perpetual, worldwide, non-exclusive, no-charge, royalty-free, irrevocable (except as stated in this section) patent license to make, have made, use, offer to sell, sell, import, and otherwise transfer the Work, where such license applies only to those patent claims licensable by such Contributor that are necessarily infringed by their Contribution(s) alone or by combination of their Contribution(s) with the Work to which such Contribution(s) was submitted. If You institute patent litigation against any entity (including a cross-claim or counterclaim in a lawsuit) alleging that the Work or a Contribution incorporated within the Work constitutes direct or contributory patent infringement, then any patent licenses granted to You under this License for that Work shall terminate as of the date such litigation is filed.

4. Redistribution.

You may reproduce and distribute copies of the Work or Derivative Works thereof in any medium, with or without modifications, and in Source or Object form, provided that You meet the following conditions:

1. You must give any other recipients of the Work or Derivative Works a copy of this License; and

2. You must cause any modified files to carry prominent notices stating that You changed the files; and

3. You must retain, in the Source form of any Derivative Works that You distribute, all copyright, patent, trademark, and attribution notices from the Source form of the Work, excluding those notices that do not pertain to any part of the Derivative Works; and

4. If the Work includes a "NOTICE" text file as part of its distribution, then any Derivative Works that You distribute must include a readable copy of the attribution notices contained within such NOTICE file, excluding those notices that do not pertain to any part of the Derivative Works, in at least one of the following places: within a NOTICE text file distributed as part of the Derivative Works; within the Source form or documentation, if provided along with the Derivative Works; or, within a display generated by the Derivative Works, if and wherever such third-party notices normally appear. The contents of the NOTICE file are for informational purposes only and do not modify the License. You may add Your own attribution notices within Derivative Works that You distribute, alongside or as an addendum to the NOTICE text from the Work, provided that such additional attribution notices cannot be construed as modifying the License.

You may add Your own copyright statement to Your modifications and may provide additional or different license terms and conditions for use, reproduction, or distribution of Your modifications, or for any such Derivative Works as a whole, provided Your use, reproduction, and distribution of the Work otherwise complies with the conditions stated in this License.

5. Submission of Contributions.

Unless You explicitly state otherwise, any Contribution intentionally submitted for inclusion in the Work by You to the Licensor shall be under the terms and conditions of this License, without any additional terms or conditions. Notwithstanding the above, nothing herein shall supersede or modify the terms of any separate license agreement you may have executed with Licensor regarding such Contributions.

6. Trademarks.

This License does not grant permission to use the trade names, trademarks, service marks, or product names of the Licensor, except as required for reasonable and customary use in describing the origin of the Work and reproducing the content of the NOTICE file.

7. Disclaimer of Warranty.

Unless required by applicable law or agreed to in writing, Licensor provides the Work (and each Contributor provides its Contributions) on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied, including, without limitation, any warranties or conditions of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A PARTICULAR PURPOSE. You are solely responsible for determining the appropriateness of using or redistributing the Work and assume any risks associated with Your exercise of permissions under this License.

8. Limitation of Liability.

In no event and under no legal theory, whether in tort (including negligence), contract, or otherwise, unless required by applicable law (such as deliberate and grossly negligent acts) or agreed to in writing, shall any Contributor be liable to You for damages, including any direct, indirect, special, incidental, or consequential damages of any character arising as a result of this License or out of the use or inability to use the Work (including but not limited to damages for loss of goodwill, work stoppage, computer failure or malfunction, or any and all other commercial damages or losses), even if such Contributor has been advised of the possibility of such damages.

9. Accepting Warranty or Additional Liability.

While redistributing the Work or Derivative Works thereof, You may choose to offer, and charge a fee for, acceptance of support, warranty, indemnity, or other liability obligations and/or rights consistent with this License. However, in accepting such obligations, You may act only on Your own behalf and on Your sole responsibility, not on behalf of any other Contributor, and only if You agree to indemnify, defend, and hold each Contributor harmless for any liability incurred by, or claims asserted against, such Contributor by reason of your accepting any such warranty or additional liability.


MIT License

Copyright (c) 2017-present atomiks

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

*/
/* verso-vars.css */
:root {
    /** Typography **/
    /* The font family used for headers, ToC entries, etc */
    --verso-structure-font-family: "Helvetica Neue", "Segoe UI", "Roboto", Arial, sans-serif;
    /* The font family used for body text */
    --verso-text-font-family: "Helvetica Neue", "Segoe UI", "Roboto", Arial, sans-serif;
    /* The font family used for code */
    --verso-code-font-family: monospace;

    /** Text colors **/
    --verso-text-color: black;
    --verso-code-color: black;
    --verso-structure-color: black;

    /** Selected items (e.g. search results) */
    --verso-selected-color: #def;

    /** Tooltips **/
    /*
    These colors are used for the tooltips that display documentation and messages for code,
    and for the equivalent popups that are shown when scripts are unavailable. The
    severity-specific tooltip colors below default to the foreground and background given
    here. The separator color draws the rule between sections of a tooltip's content.
    */
    --verso-tooltip-color: black;
    --verso-tooltip-bg-color: #e5e5e5;
    --verso-tooltip-border-color: black;
    --verso-tooltip-separator-color: #ccc;

    /** Message colors **/
    /*
    These colors are used to render Lean's feedback. Each severity (info, warning, error)
    has four sets of related styles:

    - The affected code itself: foreground and background, both at rest and while hovered,
      plus the indicator color, which draws the wavy underline marking that a message is
      present.
    - The text of the message, via the message color. This color is also used for embedded
      code.
    - The tooltip that displays the message: foreground, background, and border. The border
      color also draws the accent bar next to a message when it is shown without scripts,
      and the background also colors the tooltip's arrow.
    - The marker bar in the margin of output blocks, via the output color.
    */
    --verso-code-info-color: currentcolor;
    --verso-code-info-bg-color: transparent;
    --verso-code-info-hover-color: currentcolor;
    --verso-code-info-hover-bg-color: #4777ff;
    --verso-info-indicator-color: #4777ff;
    --verso-message-info-color: currentcolor;
    --verso-tooltip-info-color: var(--verso-tooltip-color);
    --verso-tooltip-info-bg-color: var(--verso-tooltip-bg-color);
    --verso-tooltip-info-border-color: #4777ff;
    --verso-output-info-color: var(--verso-info-indicator-color);

    --verso-code-warning-color: currentcolor;
    --verso-code-warning-bg-color: transparent;
    --verso-code-warning-hover-color: currentcolor;
    --verso-code-warning-hover-bg-color: #ffd580;
    --verso-warning-indicator-color: #e7a71d; /* 2.11 contrast ratio for white, 9.94 for black */
    --verso-message-warning-color: currentcolor;
    --verso-tooltip-warning-color: var(--verso-tooltip-color);
    --verso-tooltip-warning-bg-color: var(--verso-tooltip-bg-color);
    --verso-tooltip-warning-border-color: #ffd580;
    --verso-output-warning-color: var(--verso-warning-indicator-color);

    --verso-code-error-color: currentcolor;
    --verso-code-error-bg-color: transparent;
    --verso-code-error-hover-color: currentcolor;
    --verso-code-error-hover-bg-color: #ffb3b3;
    --verso-error-indicator-color: #ff0000;
    --verso-message-error-color: #cc0000;
    --verso-tooltip-error-color: var(--verso-tooltip-color);
    --verso-tooltip-error-bg-color: var(--verso-tooltip-bg-color);
    --verso-tooltip-error-border-color: #ffb3b3;
    --verso-output-error-color: var(--verso-error-indicator-color);

    /** Proof states **/
    /*
    These colors are used for the proof state displays that can be expanded inside of proofs
    and shown in tooltips. The toggle colors are used for the control that expands and
    collapses a proof state.
    */
    --verso-tactic-state-color: black;
    --verso-tactic-state-bg-color: white;
    --verso-tactic-state-border-color: #888888;
    --verso-tactic-toggle-color: #bbbbbb;
    --verso-tactic-toggle-checked-color: #999999;

    /** Code Highlighting **/
    /*
    These variables control the rendering of Lean code emitted by Verso. Each category that can be
    highlighted supports the customization of color, weight, style, and family.
    */
    /* Constants (e.g. `List` or `id` or `none`) */
    --verso-code-const-color: var(--verso-code-color);
    --verso-code-const-weight: normal;
    --verso-code-const-style: normal;
    --verso-code-const-font-family: var(--verso-code-font-family);

    /* Keywords/atoms (e.g. `for` or `def` or `induction`) */
    --verso-code-keyword-color: var(--verso-code-color);
    --verso-code-keyword-weight: bold;
    --verso-code-keyword-style: normal;
    --verso-code-keyword-font-family: var(--verso-code-font-family);

    /* Local bindings (e.g. `x` in `let x := 5`) */
    --verso-code-var-color: var(--verso-code-color);
    --verso-code-var-weight: normal;
    --verso-code-var-style: italic;
    --verso-code-var-font-family: var(--verso-code-font-family);

    /* The background of interactive code while hovered (hoverable tokens, occurrences of a
       hovered binding, and the tactics that expand to a proof state) */
    --verso-code-hover-bg-color: #eeeeee;
}

/* highlighter.css */


.hl.lean {
  white-space: pre;
  font-weight: normal;
  font-style: normal;
  font-size: inherit;
}

.hl.lean .keyword {
  color: var(--verso-code-keyword-color,);
  font-weight: var(--verso-code-keyword-weight, bold);
  font-style: var(--verso-code-keyword-style, normal);
  font-family: var(--verso-code-keyword-font-family,);
}

.hl.lean .const {
  color: var(--verso-code-const-color,);
  font-weight: var(--verso-code-const-weight, normal);
  font-style: var(--verso-code-const-style, normal);
  font-family: var(--verso-code-const-font-family,);
}

.hl.lean .var {
  color: var(--verso-code-var-color,);
  font-weight: var(--verso-code-var-weight, normal);
  font-style: var(--verso-code-var-style, italic);
  font-family: var(--verso-code-var-font-family,);

  position: relative;
}

.hl.lean .literal, .hl.lean .unknown {
  color: var(--verso-code-color,);
  font-weight: normal;
  font-style: normal;
  font-family: var(--verso-code-font-family,);
}

/* These lexically-classified token kinds default to the `.unknown` appearance. The rule follows
   `.const` so it wins for the `anon-ctor` tokens, which also carry the `const` class. */
.hl.lean .anon-ctor,
.hl.lean .number,
.hl.lean .char,
.hl.lean .comment,
.hl.lean .punctuation,
.hl.lean .delim,
.hl.lean .wildcard {
  color: var(--verso-code-color,);
  font-weight: normal;
  font-style: normal;
  font-family: var(--verso-code-font-family,);
}

.hover-container {
  width: 0;
  height: 0;
  position: relative;
  display: inline;
}

.hl.lean a {
  color: inherit;
  text-decoration: currentcolor underline dotted;
}

.hl.lean a:hover {
  text-decoration: currentcolor underline solid;
}

/* Links inside tooltips show their underline only when hovered. */
.tippy-box .hl.lean a {
  text-decoration: none;
}

.hl.lean .hover-info {
  white-space: normal;
}

.hl.lean .token .hover-info {
  display: none;
  position: absolute;
  color: var(--verso-tooltip-color, black);
  background-color: var(--verso-tooltip-bg-color, #e5e5e5);
  border: 1px solid var(--verso-tooltip-border-color, black);
  padding: 0.5rem;
  z-index: 300;
}

.hl.lean .hover-info.messages {
  max-height: 10rem;
  overflow-y: auto;
  overflow-x: hidden;
  scrollbar-gutter: stable;
  padding: 0 0.5rem 0 0;
  display: block;
}

.hl.lean .hover-info code {
  white-space: pre-wrap;
  background: none;
}

.hl.lean .hover-info code:not(.verso-message) {
  color: var(--verso-tooltip-color, black);
}

.hl.lean .hover-info.messages > code {
  padding: 0.5rem;
  display: block;
  width: fit-content;
}

.hl.lean .hover-info.messages > code:only-child {
  margin: 0;
}

.hl.lean .hover-info.messages > code {
  margin: 0.1rem;
}

.hl.lean .hover-info.messages > code:not(:first-child) {
  margin-top: 0rem;
}

.hl.lean {
}

.hl.lean.block {
  display: block;
}

.hl.lean.inline {
  display: inline;
  white-space: pre-wrap;
}

.hl.lean * {
}

.hl.lean .token {
  transition: all 0.25s; /* Slight fade for highlights */
}

@media (hover: hover) {
  /* Hovered content nested in a collapsed tactic region keeps its plain background: the
     region's proof state is the tooltip that appears there, and its label is what
     highlights. `:where` keeps the exclusion out of the specificity computation. */
  .hl.lean .token.binding-hl,
  .hl.lean :is(.literal, .token.typed, .token[data-verso-hover]):hover:not(:where(.tactic:has(> .tactic-toggle:not(:checked)) > label *)) {
    background-color: var(--verso-code-hover-bg-color, #eeeeee);
    border-radius: 2px;
    transition: none;
  }

  /* Within a hovered message span, token hover backgrounds are removed so the span's own
     hover background shows across the whole span. The exception is a hovered documented
     token, which keeps its background because its tooltip is the one shown. */
  .hl.lean .has-info:hover .token.binding-hl:not(:hover),
  .hl.lean .has-info:hover .token.binding-hl:not([data-verso-hover]),
  .hl.lean .has-info:hover .literal:hover:not([data-verso-hover]),
  .hl.lean .has-info:hover .token.typed:hover:not([data-verso-hover]) {
    background-color: transparent;
  }
}


.hl.lean .has-info .token:not(.tactic-state):not(.tactic-state *), .hl.lean .has-info .inter-text:not(.tactic-state):not(.tactic-state *) {
  text-decoration-style: wavy;
  text-decoration-line: underline;
  text-decoration-thickness: from-font;
  text-decoration-skip-ink: none;
}

/*
The underline color comes from the nearest enclosing message span: each severity's span rule
sets `--verso--region-indicator-color`, which inherits, so a region nested inside one of
another severity keeps its own indicator color.
*/
.hl.lean .has-info :not(.tactic-state):not(.tactic-state *) {
  text-decoration-color: var(--verso--region-indicator-color);
}

.hl.lean .has-info .hover-info {
  display: none;
  position: absolute;
  transform: translate(0.25rem, 0.3rem);
  color: var(--verso-tooltip-color, black);
  border: 1px solid var(--verso-tooltip-border-color, black);
  padding: 0.5rem;
  z-index: 400;
  text-align: left;
}

.hl.lean .has-info.error {
  --verso--region-indicator-color: var(--verso-error-indicator-color, #ff0000);
  --verso--region-hover-color: var(--verso-code-error-hover-color, currentcolor);
  --verso--region-hover-bg-color: var(--verso-code-error-hover-bg-color, #ffb3b3);
  color: var(--verso-code-error-color, currentcolor);
  background-color: var(--verso-code-error-bg-color, transparent);
}

/*
The hover highlight follows the tooltip: a message span highlights only when its own tooltip
is the one that appears. When a hovered nested message span, a hovered documented token, or
a hovered collapsed tactic label shows its own tooltip instead, the message span does not
highlight. A span nested in a collapsed tactic region likewise stays plain, because hovering
it shows the region's proof state; `:where` keeps that exclusion out of the specificity
computation.

The hover colors come from `--verso--region-hover-color` and `--verso--region-hover-bg-color`,
which each severity's span rule sets. This keeps the hover conditions in this one rule, and
because the properties inherit, a region nested inside one of another severity keeps its own
hover colors.
*/
@media (hover: hover) {
  .hl.lean .has-info:hover:not(:has(.has-info:hover)):not(:has([data-verso-hover]:hover)):not(:has(.tactic > label:hover + .tactic-toggle:not(:checked))):not(:where(.tactic:has(> .tactic-toggle:not(:checked)) > label *)) {
    color: var(--verso--region-hover-color, currentcolor);
    background-color: var(--verso--region-hover-bg-color, transparent);
  }
}

.hl.lean .hover-info.messages > code.error {
  background-color: var(--verso-tooltip-error-bg-color, #e5e5e5);
  border-left: 0.2rem solid var(--verso-tooltip-error-border-color, #ffb3b3);
}

/*
A tooltip that shows only messages of the box's own severity leaves severity styling to the
box itself. When messages of several severities share the tooltip, or a `mixed` tooltip
combines messages with other content (such as documentation), each message keeps its
severity accent to distinguish them.
*/
.tippy-box .hl.lean.mixed > .hover-info.messages {
  margin-bottom: 0.5rem;
}

.tippy-box[data-theme~='error'] .hl.lean:not(.mixed) .hover-info.messages:not(:has(> code.warning)):not(:has(> code.information)) > code.error {
  background: none;
  border: none;
}

.error .verso-message, .error .verso-message .token, .error .verso-message label {
  color: var(--verso-message-error-color, #cc0000);
}

.error .verso-message .case-label:has(input[type="checkbox"])::before {
  background-color: var(--verso-message-error-color, #cc0000) !important;
}

.hl.lean .has-info.warning {
  --verso--region-indicator-color: var(--verso-warning-indicator-color, #e7a71d);
  --verso--region-hover-color: var(--verso-code-warning-hover-color, currentcolor);
  --verso--region-hover-bg-color: var(--verso-code-warning-hover-bg-color, #ffd580);
  color: var(--verso-code-warning-color, currentcolor);
  background-color: var(--verso-code-warning-bg-color, transparent);
}

.hl.lean .hover-info.messages > code.warning {
  background-color: var(--verso-tooltip-warning-bg-color, #e5e5e5);
  border-left: 0.2rem solid var(--verso-tooltip-warning-border-color, #ffd580);
}

.lean-output {
  border-left: 0.2em solid transparent;
  padding: 0 0 0 0.5em;
  border-top-left-radius: 0;
  border-bottom-left-radius: 0;
}

.lean-output.error {
  border-color: var(--verso-output-error-color, var(--verso-error-indicator-color, #ff0000));
}

.lean-output.information {
  border-color: var(--verso-output-info-color, var(--verso-info-indicator-color, #4777ff));
}

.lean-output.warning {
  border-color: var(--verso-output-warning-color, var(--verso-warning-indicator-color, #e7a71d));
}

.tippy-box[data-theme~='warning'] .hl.lean:not(.mixed) .hover-info.messages:not(:has(> code.error)):not(:has(> code.information)) > code.warning {
  background: none;
  border: none;
}

.warning .verso-message, .warning .verso-message .token, .warning .verso-message label {
  color: var(--verso-message-warning-color, currentcolor);
}

.warning .verso-message .case-label:has(input[type="checkbox"])::before {
  background-color: var(--verso-message-warning-color, currentcolor) !important;
}


.hl.lean .has-info.information {
  --verso--region-indicator-color: var(--verso-info-indicator-color, #4777ff);
  --verso--region-hover-color: var(--verso-code-info-hover-color, currentcolor);
  --verso--region-hover-bg-color: var(--verso-code-info-hover-bg-color, #4777ff);
  color: var(--verso-code-info-color, currentcolor);
  background-color: var(--verso-code-info-bg-color, transparent);
}


.hl.lean .hover-info.messages > code.information {
  background-color: var(--verso-tooltip-info-bg-color, #e5e5e5);
  border-left: 0.2rem solid var(--verso-tooltip-info-border-color, #4777ff);
}

.tippy-box[data-theme~='info'] .hl.lean:not(.mixed) .hover-info.messages:not(:has(> code.error)):not(:has(> code.warning)) > code.information {
  background: none;
  border: none;
}

.information .verso-message, .information .verso-message .token, .information .verso-message label {
  color: var(--verso-message-info-color, currentcolor);
}

.information .verso-message .case-label:has(input[type="checkbox"])::before {
  background-color: var(--verso-message-info-color, currentcolor) !important;
}

.hl.lean div.docstring {
  font-family: var(--verso-text-font-family, sans-serif);
  white-space: normal;
  max-width: calc(min(40rem, 90vw));
  width: max-content;
}

.hl.lean div.docstring > :last-child {
  margin-bottom: 0;
}

.hl.lean div.docstring > :first-child {
  margin-top: 0;
}

.hl.lean .hover-info .sep {
  display: block;
  width: auto;
  margin-left: 1rem;
  margin-right: 1rem;
  margin-top: 0.5rem;
  margin-bottom: 0.5rem;
  padding: 0;
  height: 1px;
  border-top: 1px solid var(--verso-tooltip-separator-color, #ccc);
}

.hl.lean code {
  font-family: var(--verso-code-font-family);
}

.hl.lean .tactic-state {
  display: none;
  position: relative;
  width: fit-content;
  border: 1px solid var(--verso-tactic-state-border-color, #888888);
  border-radius: 0.1rem;
  padding: 0.5rem;
  font-family: sans-serif;
  color: var(--verso-tactic-state-color, black);
  background-color: var(--verso-tactic-state-bg-color, white);
}

.hl.lean.popup .tactic-state {
  position: static;
  display: block;
  width: auto;
  border: none;
  padding: 0.5rem;
  font-family: sans-serif;
  background-color: var(--verso-tactic-state-bg-color, white);
}


.hl.lean .tactic {
  position: relative;
  display: inline;
  vertical-align: top;
  /* Without these, mobile Safari will start making font sizes inconsistent when its text size adjustment feature is triggered.*/
  -webkit-text-size-adjust: 100%;
  text-size-adjust: 100%;
}

.hl.lean .tactic:has(> .tactic-toggle:checked) {
  display: inline-grid;
  grid-template-columns: 1fr;
}

.hl.lean .tactic-toggle:checked ~ .tactic-state {
  display: inline-block;
  vertical-align: top;
  grid-row: 2;
  justify-self: start;
}

.hl.lean .tactic > label {
  position: relative;
  grid-row: 1;
  display: inline;
}

@media (hover: hover) {
  /* Highlight a region on hover only when its own toggle is unchecked, and only the innermost
     hovered region: `label:hover` bubbles to ancestor labels, so suppress the highlight on a region
     whose label contains a more deeply nested hovered tactic label. The region's proof state is the
     tooltip for everything else in its label, so the label keeps its highlight while any of that
     content is hovered. */
  .hl.lean .tactic:has(> .tactic-toggle:not(:checked)) > label:hover:not(:has(.tactic > label:hover)) {
    background-color: var(--verso-code-hover-bg-color, #eeeeee);
  }
}

.hl.lean .tactic-toggle {
  position: absolute;
  top: 0;
  left: 0;
  opacity: 0;
  height: 0;
  width: 0;
  z-index: -10;
}

.hl.lean .tactic > label::after {
  content: "";
  border: 1px solid var(--verso-tactic-toggle-color, #bbbbbb);
  /* These need to be em, not rem, to scale with the font */
  border-radius: 1em;
  height: 0.25em;
  vertical-align: middle;
  width: 0.6em;
  margin-left: 0.1em;
  margin-right: 0.1em;
  display: inline-block;
  transition: all 0.5s;
}

/*
@media (hover: hover) {
  .hl.lean .tactic > label:hover::after {
    border: 1px solid #aaaaaa;
    background-color: #aaaaaa;
    transition: all 0.5s;
  }
}
*/

.hl.lean .tactic > label:has(+ .tactic-toggle:checked)::after {
  border: 1px solid var(--verso-tactic-toggle-checked-color, #999999);
  background-color: var(--verso-tactic-toggle-checked-color, #999999);
  transition: all 0.5s;
}

.hl.lean .tactic-state .goal + .goal {
  margin-top: 1.5em;
}

/*
Some CSS frameworks customize details/summary in ways not compatible with Verso's output.
*/

.hl.lean details {
  display: block !important;
  margin: 0;
}

.hl.lean details summary {
  display: list-item !important;
  margin: 0;
}

.hl.lean details summary:focus {
  outline: none;
  outline-offset: none;
  color: inherit;
}

.hl.lean ul > li {
  margin-bottom: 0;
}

.hl.lean details summary::marker {
  display: inline !important;
}

.hl.lean details > summary:first-of-type {
  list-style-type: disclosure-closed;
  list-style-position: inside;
}

.hl.lean details[open] > summary:first-of-type {
  list-style-type: disclosure-open;
}

.hl.lean details summary::before, .hl.lean details summary::after {
  content: "" !important;
  background: none;
  display: none;
}

.hl.lean .tactic-state summary {
  /* These need to be em, not rem, to scale with the font */
  margin-left: -0.5em;
}

.hl.lean .tactic-state details {
  /* These need to be em, not rem, to scale with the font */
  padding-left: 0.5em;
}

.hl.lean .case-label {
  display: block;
  position: relative;
}

.hl.lean .case-label input[type="checkbox"] {
  position: absolute;
  top: 0;
  left: 0;
  opacity: 0;
  height: 0;
  width: 0;
  z-index: -10;
}

.hl.lean .case-label:has(input[type="checkbox"])::before {
  display: inline-block;
  background-color: currentcolor;
  content: ' ';
  transition: ease 0.2s;
  margin-right: 0.7em;
  clip-path: polygon(100% 0, 0 0, 50% 100%);
  width: 0.6em;
  height: 0.6em;
  vertical-align: middle;
}

.hl.lean .case-label:has(input[type="checkbox"]:not(:checked))::before {
  transform: rotate(-90deg);
}

.hl.lean .case-label:has(input[type="checkbox"]) {

}

.hl.lean .case-label:has(input[type="checkbox"]:checked) {

}


.hl.lean .labeled-case > :not(:first-child) {
  max-height: 0px;
  display: block;
  overflow: hidden;
  transition: max-height 0.1s ease-in;
  /* These need to be em, not rem, to scale with the font */
  margin-left: 0.5em;
  margin-top: 0.1em;
}

.hl.lean .labeled-case:has(.case-label input[type="checkbox"]:checked) > :not(:first-child) {
  max-height: 100%;
}


.hl.lean .goal-name::before {
  font-style: normal;
  content: "case ";
}

.hl.lean .goal-name {
  font-style: italic;
  font-family: var(--verso-code-font-family);
  color: inherit;
}

.hl.lean .hypotheses {
  display: table;
}

.hl.lean .hypothesis {
  display: table-row;
}

.hl.lean .hypothesis > * {
  display: table-cell;
}


.hl.lean .hypotheses .colon {
  text-align: center;
  /* This needs to be em, not rem, to scale with the font */
  min-width: 1em;
}

.hl.lean .hypotheses .name {
  text-align: right;
}

.hl.lean .hypotheses .name,
.hl.lean .hypotheses .type,
.hl.lean .conclusion .type {
  font-family: var(--verso-code-font-family);
}

.tippy-box {
  /* Without these, mobile Safari will start making font sizes inconsistent when its text size adjustment feature is triggered.*/
  -webkit-text-size-adjust: 100%;
  text-size-adjust: 100%;
}

/*
Tippy's stylesheet paints each arrow's fill triangle with the arrow element's `color` (its
placement-specific `::before` rules use `border-color: initial`, which is `currentcolor`),
and its border extension paints the outline triangle with the box's border color. Setting
`color` on the arrow therefore matches it to the tooltip background for every placement.
*/
.tippy-box[data-theme~='lean'] {
  background-color: var(--verso-tooltip-bg-color, #e5e5e5);
  color: var(--verso-tooltip-color, black);
  border: 1px solid var(--verso-tooltip-border-color, black);
}
.tippy-box[data-theme~='lean'] > .tippy-arrow {
  color: var(--verso-tooltip-bg-color, #e5e5e5);
}

.tippy-box[data-theme~='message'][data-placement^='top'] > .tippy-arrow::before {
  border-width: 11px 11px 0;
}
.tippy-box[data-theme~='message'][data-placement^='top'] > .tippy-arrow::after {
  bottom: -11px;
  border-width: 11px 11px 0;
}
.tippy-box[data-theme~='message'][data-placement^='bottom'] > .tippy-arrow::before {
  border-width: 0 11px 11px;
}
.tippy-box[data-theme~='message'][data-placement^='bottom'] > .tippy-arrow::after {
  top: -11px;
  border-width: 0 11px 11px;
}
.tippy-box[data-theme~='message'][data-placement^='left'] > .tippy-arrow::before {
  border-width: 11px 0 11px 11px;
}
.tippy-box[data-theme~='message'][data-placement^='left'] > .tippy-arrow::after {
  right: -11px;
  border-width: 11px 0 11px 11px;
}

.tippy-box[data-theme~='message'][data-placement^='right'] > .tippy-arrow::before {
  border-width: 11px 11px 11px 0;
}
.tippy-box[data-theme~='message'][data-placement^='right'] > .tippy-arrow::after {
  left: -11px;
  border-width: 11px 11px 11px 0;
}



.tippy-box[data-theme~='warning'] {
  background-color: var(--verso-tooltip-warning-bg-color, #e5e5e5);
  color: var(--verso-tooltip-warning-color, black);
  border: 3px solid var(--verso-tooltip-warning-border-color, #ffd580);
}
.tippy-box[data-theme~='warning'] > .tippy-arrow {
  color: var(--verso-tooltip-warning-bg-color, #e5e5e5);
}

.tippy-box[data-theme~='error'] {
  background-color: var(--verso-tooltip-error-bg-color, #e5e5e5);
  color: var(--verso-tooltip-error-color, black);
  border: 3px solid var(--verso-tooltip-error-border-color, #ffb3b3);
}
.tippy-box[data-theme~='error'] > .tippy-arrow {
  color: var(--verso-tooltip-error-bg-color, #e5e5e5);
}

.tippy-box[data-theme~='info'] {
  background-color: var(--verso-tooltip-info-bg-color, #e5e5e5);
  color: var(--verso-tooltip-info-color, black);
  border: 3px solid var(--verso-tooltip-info-border-color, #4777ff);
}
.tippy-box[data-theme~='info'] > .tippy-arrow {
  color: var(--verso-tooltip-info-bg-color, #e5e5e5);
}

.tippy-box[data-theme~='tactic'] {
  background-color: var(--verso-tactic-state-bg-color, white);
  color: var(--verso-tactic-state-color, black);
  border: 1px solid var(--verso-tactic-state-border-color, #888888);
}
.tippy-box[data-theme~='tactic'] > .tippy-arrow {
  color: var(--verso-tactic-state-bg-color, white);
}

.extra-doc-links {
  list-style-type: none;
  margin-left: 0;
  padding: 0;
}

.extra-doc-links > li {
  display: inline-block;
}

.extra-doc-links > li:not(:last-child)::after {
  content: '|';
  display: inline-block;
  margin: 0 0.25em;
}

.verso-message .trace {
  display: block;
}

.verso-message .trace > summary::marker {
  color: var(--verso-text-color, black);
}

.verso-message .trace-children {
  margin: 0;
  padding: 0;
}

.verso-message .trace-children > li {
  list-style-type: none;
  margin-left: 1.5em;
}

.verso-message .trace-children > li:not(:has(.trace)) {
  margin-left: 0;
}

.verso-message .trace-class {
  color: color-mix(in srgb, currentColor 70%, transparent);
  font-weight: bold;
  margin: 0;
  padding: 0;
}

.verso-message .text {
  white-space: pre-wrap;
}


/* code.css */
/* Verso variables */

:root {
    --verso-code-font-family: "Monaco", "Menlo", "Ubuntu Mono", monospace;
    --verso-text-font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}

/* Reset and base styles */
* {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}

body {
    font-family: var(--verso-text-font-family);
    line-height: 1.6;
    color: #333;
    height: 100vh;
    overflow: hidden;
}

/* Layout container */
.layout {
    display: flex;
    height: 100vh;
}

/* Sidebar */
.sidebar {
    width: 300px;
    background: #f8f9fa;
    border-right: 1px solid #e9ecef;
    overflow-y: auto;
    flex-shrink: 0;
}

.sidebar-content {
    padding: 1rem;
}

/* Mobile-specific sidebar content spacing */
@media (max-width: 768px) {
    .sidebar-content {
        padding-top: 4rem;
        /* Space for hamburger menu */
    }
}

/* Module tree styling */
.module-tree {
    font-family: var(--verso-code-font-family);
    font-size: 0.875rem;
}

.module-tree details {
    margin-left: 1rem;
}

/*
.module-tree details[open]>summary {
  margin-bottom: 0.5rem;
}
*/

.module-tree summary {
    cursor: pointer;
    padding: 0.25rem 0.5rem;
    border-radius: 0.25rem;
    list-style: none;
    position: relative;
    transition: background-color 0.2s ease;
}

.module-tree summary:hover {
    background-color: #e9ecef;
}

.module-tree summary a {
    color: #0066cc;
    text-decoration: none;
}

.module-tree summary a:hover {
    text-decoration: underline;
}

.module-tree summary.current {
    background-color: #0066cc;
    color: white;
}

.module-tree summary.current a {
    color: white;
}

.module-tree summary::before {
    content: "▶";
    position: absolute;
    left: -0.75rem;
    transition: transform 0.2s ease;
    font-size: 0.75rem;
    color: #6c757d;
}

.module-tree details[open] > summary::before {
    transform: rotate(90deg);
}

.module-tree .leaf {
    margin-left: 1rem;
    padding: 0.25rem 0.5rem;
    border-radius: 0.25rem;
}

.module-tree .leaf a {
    color: #0066cc;
    text-decoration: none;
}

.module-tree .leaf a:hover {
    text-decoration: underline;
}

.module-tree .current {
    background-color: #0066cc;
    color: white;
}

.module-tree .current a {
    color: white;
}

/* Main content area */
.main-area {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

/* Title bar */
.title-bar {
    background: #fff;
    border-bottom: 1px solid #e9ecef;
    padding: 1rem 1.5rem;
    flex-shrink: 0;
    position: relative;
}

.breadcrumbs {
    list-style: none;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    font-size: 0.9rem;
    font-family: "Monaco", "Menlo", "Ubuntu Mono", monospace;
}

.breadcrumbs li {
    display: flex;
    align-items: center;
}

.breadcrumbs li:not(:last-child)::after {
    content: "·";
    margin: 0;
    color: #6c757d;
    font-weight: bold;
}

.breadcrumbs a {
    color: #0066cc;
    text-decoration: none;
    padding: 0.25rem 0.5rem;
    border-radius: 0.25rem;
    transition: background-color 0.2s ease;
}

.breadcrumbs a:hover {
    background-color: #e3f2fd;
    text-decoration: underline;
}

.breadcrumbs .current {
    font-weight: 600;
    color: #495057;
    padding: 0.25rem 0.5rem;
    border-radius: 0.25rem;
}

/* Code content area */
.code-content {
    flex: 1;
    overflow-y: auto;
    padding: 2rem;
    background: #ffffff;
    color: #24292e;
    font-family: "Monaco", "Menlo", "Ubuntu Mono", monospace;
    font-size: 1rem;
}

.code-content .verso-text,
.code-content .md-text {
    /* Indent by the width of "O" in the monospace font, times the specified indentation */
    margin-left: calc(var(--indent, 0) * 1ch);
    padding: 0.25rem 0.5rem;
    max-width: 40em;
    border: 1px solid #ddd;
    border-radius: 1rem;
    width: max-content;
}

.code-content .verso-text::before,
.code-content .md-text::before {
    content: "/--";
    display: block;
    width: min-content;
}

.code-content .verso-text.mod-doc::before,
.code-content .md-text.mod-doc::before {
    content: "/-!";
    display: block;
    width: min-content;
}

.code-content .verso-text::after,
.code-content .md-text::after {
    content: "-/";
    display: block;
    width: min-content;
}

.code-content .verso-text > *,
.code-content .md-text > * {
    width: max-content;
    max-width: 100%;
}

.code-content .verso-text > *,
.code-content .md-text > * {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    margin-bottom: 0.5rem;
    display: block;
}

.code-content .verso-text > *:last-child,
.code-content .md-text > *:last-child {
    margin-bottom: 0;
}

.code-content .verso-text ul,
.code-content .md-text ul {
    padding-left: 1.5em;
}

.code-content .verso-text ul > li,
.code-content .md-text ul > li {
    margin-bottom: 0.5rem;
}

.code-content .verso-text ul > li:last-child,
.code-content .md-text ul > li:last-child {
    margin-bottom: 0;
}

.code-content .verso-text dt,
.code-content .md-text dt {
    padding-left: 0.5em;
    font-weight: bold;
}

.code-content .verso-text dd,
.code-content .md-text dd {
    padding-left: 1.5em;
}

.code-content .verso-text dd:not(:last-child),
.code-content .md-text dd:not(:last-child) {
    margin-bottom: 0.5rem;
}

pre,
.code-content .verso-text pre {
    font-family: var(--verso-code-font-family, monospace);
}

/* Hamburger menu (hidden by default) */
.menu-toggle {
    display: none;
}

.hamburger {
    display: none;
    position: fixed;
    top: 1rem;
    left: 1rem;
    z-index: 1001;
    background: #fff;
    border: 1px solid #dee2e6;
    border-radius: 0.375rem;
    padding: 0.5rem;
    cursor: pointer;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
}

.hamburger span {
    display: block;
    width: 20px;
    height: 2px;
    background: #333;
    margin: 4px 0;
    transition: 0.3s;
}

/* Mobile styles */
@media (max-width: 768px) {
    .hamburger {
        display: block;
    }

    .sidebar {
        position: fixed;
        top: 0;
        left: -300px;
        height: 100vh;
        z-index: 1000;
        transition: left 0.3s ease;
        box-shadow: 2px 0 10px rgba(0, 0, 0, 0.1);
    }

    .menu-toggle:checked + .hamburger + .layout .sidebar {
        left: 0;
    }

    .menu-toggle:checked + .hamburger span:nth-child(1) {
        transform: translate(0px, 6px) rotate(45deg);
    }

    .menu-toggle:checked + .hamburger span:nth-child(2) {
        opacity: 0;
    }

    .menu-toggle:checked + .hamburger span:nth-child(3) {
        transform: translate(0px, -6px) rotate(-45deg);
    }

    .main-area {
        width: 100%;
    }

    .title-bar {
        padding-left: 4rem;
        /* Space for hamburger */
    }

    /* Overlay when menu is open */
    .menu-toggle:checked + .hamburger + .layout::after {
        content: "";
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: rgba(0, 0, 0, 0.5);
        z-index: 999;
    }
}

/* tippy-border.css */
.tippy-box{border:1px transparent}.tippy-box[data-placement^=top]>.tippy-arrow:after{border-top-color:inherit;border-width:8px 8px 0;bottom:-8px;left:0}.tippy-box[data-placement^=bottom]>.tippy-arrow:after{border-bottom-color:inherit;border-width:0 8px 8px;top:-8px;left:0}.tippy-box[data-placement^=left]>.tippy-arrow:after{border-left-color:inherit;border-width:8px 0 8px 8px;right:-8px;top:0}.tippy-box[data-placement^=right]>.tippy-arrow:after{border-width:8px 8px 8px 0;left:-8px;top:0;border-right-color:inherit}.tippy-box[data-placement^=top]>.tippy-svg-arrow>svg:first-child:not(:last-child){top:17px}.tippy-box[data-placement^=bottom]>.tippy-svg-arrow>svg:first-child:not(:last-child){bottom:17px}.tippy-box[data-placement^=left]>.tippy-svg-arrow>svg:first-child:not(:last-child){left:12px}.tippy-box[data-placement^=right]>.tippy-svg-arrow>svg:first-child:not(:last-child){right:12px}.tippy-arrow{border-color:inherit}.tippy-arrow:after{content:"";z-index:-1;position:absolute;border-color:transparent;border-style:solid}"""
