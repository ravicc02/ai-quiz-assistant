const path = require("node:path");

function resolveDataRoot({ env = process.env, home, product }) {
  return env.MUMU_DATA
    || env.ANSWER_STUDIO_DATA
    || path.join(home, product.dataDirectory);
}

module.exports = { resolveDataRoot };
