(function (root) {
  "use strict";
  function money(value, currency) {
    if (value === null || value === undefined) return "尚未确认";
    if (!["CNY", "USD"].includes(currency)) throw new Error("预算币种尚未确认");
    return (currency === "CNY" ? "¥" : "$") + Number(value).toFixed(4);
  }
  function formPayload(currency, monthly, server) {
    if (!["CNY", "USD"].includes(currency)) throw new Error("预算币种尚未确认，请刷新页面");
    return currency === "CNY"
      ? {monthly_cny: monthly, server_cny: server === "" ? null : server}
      : {monthly_usd: monthly, server_usd: server === "" ? null : server};
  }
  const api = {money, formPayload};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.XiaoNuoBudget = api;
})(globalThis);
