// The "Vulkan" capability line under a GPU, shared by Settings (GPU device tuning) and the Nodes page.
//
// Takes a GPU's `vulkan` check from the API ({ok, title, why, fix}) and returns HTML: a ✓ line with the device, or
// a ✕ line with the cause and a "Why?" link that opens a panel with the reason and the fix. Needs escapeHtml and
// copyVulkanDiagnosticBundle from app.js. The click handlers are delegated, so a redraw of the list keeps them.
(function () {
    // options.bundle: add "Copy diagnostic bundle" to the panel. That bundle is this machine's, so a remote node's
    // GPU leaves it out.
    function vulkanLine(vulkan, options) {
        if (!vulkan) return '';
        if (vulkan.ok) {
            return '<div class="vk-line"><span class="vk-ok" aria-hidden="true">✓</span> Vulkan <span class="vk-detail">'
                + escapeHtml(vulkan.why || '') + '</span></div>';
        }
        const copy = options && options.bundle
            ? '<button type="button" class="btn btn-sm btn-outline-secondary mt-1 vk-copy"><i class="bi bi-clipboard me-1"></i>Copy diagnostic bundle</button>'
            : '';
        return '<div class="vk-line"><span class="vk-bad" aria-hidden="true">✕</span> Vulkan (credits text, Dolby Vision 5) '
            + '<span class="vk-detail">' + escapeHtml(vulkan.title || '') + '</span> '
            + '<button type="button" class="btn btn-link btn-sm p-0 align-baseline vk-why-toggle" aria-expanded="false">Why?</button></div>'
            + '<div class="vk-why d-none"><div><strong>Why:</strong> ' + escapeHtml(vulkan.why || '') + '</div>'
            + '<div><strong>What to do:</strong> ' + escapeHtml(vulkan.fix || '') + '</div>' + copy + '</div>';
    }

    document.addEventListener('click', function (event) {
        const toggle = event.target.closest('.vk-why-toggle');
        if (toggle) {
            const panel = toggle.closest('.vk-line').nextElementSibling;
            const open = panel.classList.toggle('d-none') === false;
            toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
            return;
        }
        const copy = event.target.closest('.vk-copy');
        if (copy) copyVulkanDiagnosticBundle(copy);
    });

    window.MPGShared = window.MPGShared || {};
    window.MPGShared.vulkanLine = vulkanLine;
})();
