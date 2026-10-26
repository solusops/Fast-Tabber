function exportRows(rows, format, grouped = false) {
  const safe = value => {
    let text = String(value ?? "");
    if (/^[=+\-@\t\r]/.test(text)) text = "'" + text;
    return '"' + text.replaceAll('"', '""') + '"';
  };
  if (grouped && format === "txt") {
    const groups = new Map();
    for (const row of rows) {
      const key = row.groupKey;
      if (!groups.has(key)) groups.set(key, { title: row.folder, urls: [] });
      groups.get(key).urls.push(row.url);
    }
    return [...groups.values()].map(group => `[${String(group.title).replace(/[\r\n]/g, " ")}]\n${group.urls.join("\n")}`).join("\n\n") + "\n";
  }
  return format === "csv" ? (grouped ? "title,url,group\r\n" : "title,url,folder\r\n") + rows.map(row => [row.title, row.url, row.folder].map(safe).join(",")).join("\r\n") + "\r\n"
    : rows.map(row => row.url).join("\n") + "\n";
}
