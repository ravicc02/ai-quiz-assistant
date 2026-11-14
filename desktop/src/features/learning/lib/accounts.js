export function filterLearningAccounts(accounts, query, platformLabel = () => "") {
  const needle = String(query || "").trim().toLocaleLowerCase();
  if (!needle) return accounts;
  return accounts.filter(account => [
    account.name,
    account.platform,
    platformLabel(account.platform),
    account.status === "ready" ? "已授权 就绪" : "待授权",
  ].join(" ").toLocaleLowerCase().includes(needle));
}
