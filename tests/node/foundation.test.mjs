import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const packageJson = JSON.parse(await readFile(new URL("../../package.json", import.meta.url)));
const packageLock = JSON.parse(await readFile(new URL("../../package-lock.json", import.meta.url)));

test("Node foundation stays on Node 24 and npm 11", () => {
  assert.deepEqual(packageJson.engines, { node: ">=24 <25", npm: ">=11 <12" });
  assert.deepEqual(packageLock.packages[""].engines, packageJson.engines);
  assert.equal(packageLock.lockfileVersion, 3);
});

test("P01 introduces no Node dependency supply chain", () => {
  assert.equal(packageJson.dependencies, undefined);
  assert.equal(packageJson.devDependencies, undefined);
  assert.deepEqual(Object.keys(packageLock.packages), [""]);
});
