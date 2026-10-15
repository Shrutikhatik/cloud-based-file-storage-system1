
cat > static/js/script.js <<'EOF'
const search = document.getElementById("search");
const fileInput = document.getElementById("fileInput");
const fileName = document.getElementById("fileName");

if (fileInput && fileName) {
  fileInput.addEventListener("change", () => {
    fileName.textContent = fileInput.files.length
      ? "Selected: " + fileInput.files[0].name
      : "No file selected";
  });
}

if (search) {
  search.addEventListener("input", () => {
    const query = search.value.toLowerCase();
    document.querySelectorAll("#fileTable tr[data-name]").forEach(row => {
      row.style.display = row.dataset.name.includes(query) ? "" : "none";
    });
  });
}

document.querySelectorAll(".filter").forEach(button => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".filter").forEach(item => item.classList.remove("active"));
    button.classList.add("active");

    const filter = button.dataset.filter;
    document.querySelectorAll("#fileTable tr[data-name]").forEach(row => {
      const ext = row.dataset.type || "";
      const isImage = ["png", "jpg", "jpeg", "gif", "webp"].includes(ext);
      const isVideo = ["mp4", "mov", "webm"].includes(ext);
      const isDocument = ["pdf", "doc", "docx", "txt", "xls", "xlsx", "ppt", "pptx", "csv"].includes(ext);

      row.style.display =
        filter === "all" ||
        (filter === "image" && isImage) ||
        (filter === "video" && isVideo) ||
        (filter === "pdf" && isDocument)
          ? ""
          : "none";
    });
  });
});

const rows = document.querySelectorAll("#fileTable tr[data-name]");
const totalFiles = document.getElementById("totalFiles");
if (totalFiles && rows.length) {
  totalFiles.textContent = rows.length;
}

const storageUsed = document.getElementById("storageUsed");
if (storageUsed) {
  let bytes = 0;
  document.querySelectorAll("#fileTable tr[data-name] td:nth-child(3)").forEach(cell => {
    const value = parseFloat(cell.textContent);
    if (!Number.isNaN(value)) bytes += value * 1024;
  });

  storageUsed.textContent = bytes >= 1024 * 1024
EOF
