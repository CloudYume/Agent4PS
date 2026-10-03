import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const messages = [];
class FakeXHR {}
FakeXHR.prototype.open = function () {};
FakeXHR.prototype.send = function () {};

const responses = [
  { submission_id: "123" },
  { submission_id: "123", status_code: 10, status_msg: "Accepted", status_runtime: "7 ms", status_memory: "19.20 MB" },
  { data: { submitCode: { submissionId: "124" } } },
  { submission_id: "125" },
];
class FakeCustomEvent {
  constructor(type, options) {
    this.type = type;
    this.detail = options.detail;
  }
}
const window = {
  dispatchEvent: (event) => { messages.push(JSON.parse(event.detail)); },
  fetch: async () => {
    const data = responses.shift();
    return { json: async () => data, clone: () => ({ json: async () => data }) };
  },
};
const context = {
  window,
  location: { origin: "https://leetcode.cn" },
  URLSearchParams,
  URL,
  FormData,
  Request,
  XMLHttpRequest: FakeXHR,
  CustomEvent: FakeCustomEvent,
};
vm.runInNewContext(readFileSync(new URL("../extension/page-hook.js", import.meta.url), "utf8"), context);

await window.fetch("/problems/two-sum/submit/", {
  method: "POST",
  body: JSON.stringify({ typed_code: "class Solution: pass" }),
});
await new Promise((resolve) => setTimeout(resolve, 0));
await window.fetch("/submissions/detail/123/check/");
await new Promise((resolve) => setTimeout(resolve, 0));

assert.equal(messages[0].type, "request");
assert.equal(messages[0].detail.code, "class Solution: pass");
assert.equal(messages[1].type, "issued");
assert.equal(messages[1].detail.submission_id, "123");
assert.equal(messages[2].type, "terminal");
assert.equal(messages[2].detail.status_code, 10);
assert.equal(messages[2].detail.status_runtime, "7 ms");
assert.equal(messages[2].detail.status_memory, "19.20 MB");

await window.fetch(new URL("https://leetcode.cn/graphql/"), {
  method: "POST",
  body: JSON.stringify({ operationName: "submitCode", variables: { input: { typedCode: "nested code" } } }),
});
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(messages[3].detail.code, "nested code");
assert.equal(messages[4].detail.submission_id, "124");

const form = new FormData();
form.set("data_json", JSON.stringify({ typed_code: "form code" }));
await window.fetch("/problems/two-sum/submit/", { method: "POST", body: form });
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(messages[5].detail.code, "form code");
assert.equal(messages[6].detail.submission_id, "125");
console.log("extension hook test passed");
