import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { resolveDataRoot } from '../electron/data-root.cjs';

const product = { dataDirectory: '.mumu' };

test('the default Mumu data root lives under the user home directory', () => {
  assert.equal(
    resolveDataRoot({ env:{}, home:'C:\\Users\\Student', product }),
    path.join('C:\\Users\\Student', '.mumu'),
  );
});

test('explicit data roots keep the MUMU_DATA then legacy precedence', () => {
  assert.equal(resolveDataRoot({
    env:{ MUMU_DATA:'primary', ANSWER_STUDIO_DATA:'legacy' },
    home:'C:\\Users\\Student',
    product,
  }), 'primary');
  assert.equal(resolveDataRoot({
    env:{ ANSWER_STUDIO_DATA:'legacy' },
    home:'C:\\Users\\Student',
    product,
  }), 'legacy');
});
