/**
 * OasisAgent Service Map — D3 force-directed topology graph.
 *
 * Loads topology from window.__TOPOLOGY_DATA__ (server-rendered)
 * or fetches from /ui/service-map/data.
 *
 * Node colors by entity_type:
 *   network_device = blue, service = green, host = gray,
 *   proxy = orange, monitor = purple
 *
 * Supports: drag, zoom/pan, click-to-inspect, manual/auto badge.
 */

(function () {
  "use strict";

  // -----------------------------------------------------------------------
  // Constants
  // -----------------------------------------------------------------------

  var TYPE_COLORS = {
    network_device: "#3b82f6",
    service: "#22c55e",
    host: "#6b7280",
    proxy: "#f97316",
    monitor: "#a855f7",
  };

  var DEFAULT_COLOR = "#94a3b8";
  var NODE_RADIUS = 24;
  var LABEL_OFFSET = NODE_RADIUS + 10;
  var ARROW_SIZE = 8;

  // -----------------------------------------------------------------------
  // State
  // -----------------------------------------------------------------------

  var svg, container, simulation;
  var linkGroup, nodeGroup, labelGroup;
  var width, height;
  var currentData = { nodes: [], links: [] };

  // -----------------------------------------------------------------------
  // Helpers
  // -----------------------------------------------------------------------

  function nodeColor(d) {
    return TYPE_COLORS[d.type] || DEFAULT_COLOR;
  }

  function getCsrfToken() {
    var match = document.cookie.match(/(?:^|;\s*)oasis_csrf_token=([^;]+)/);
    return match ? match[1] : "";
  }

  function apiRequest(method, url, body) {
    var opts = {
      method: method,
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrfToken(),
      },
    };
    if (body) {
      opts.body = JSON.stringify(body);
    }
    return fetch(url, opts).then(function (resp) {
      if (!resp.ok) {
        return resp.json().then(function (err) {
          throw new Error(err.detail || resp.statusText);
        });
      }
      return resp.json();
    });
  }

  // -----------------------------------------------------------------------
  // Initialization
  // -----------------------------------------------------------------------

  function init() {
    svg = d3.select("#service-map-svg");
    if (svg.empty()) return;

    var rect = svg.node().parentElement.getBoundingClientRect();
    width = rect.width;
    height = rect.height;

    svg.attr("viewBox", [0, 0, width, height]);

    // Arrow marker
    svg
      .append("defs")
      .append("marker")
      .attr("id", "arrowhead")
      .attr("viewBox", "0 -5 10 10")
      .attr("refX", NODE_RADIUS + ARROW_SIZE + 2)
      .attr("refY", 0)
      .attr("markerWidth", ARROW_SIZE)
      .attr("markerHeight", ARROW_SIZE)
      .attr("orient", "auto")
      .append("path")
      .attr("d", "M0,-5L10,0L0,5")
      .attr("fill", "#9ca3af");

    // Zoom behavior
    var zoom = d3
      .zoom()
      .scaleExtent([0.1, 4])
      .on("zoom", function (event) {
        container.attr("transform", event.transform);
      });

    svg.call(zoom);

    // Store zoom for fitView
    svg.__zoom_behavior = zoom;

    // Container for zoomable content
    container = svg.append("g").attr("class", "graph-container");

    // Draw order: links, nodes, labels
    linkGroup = container.append("g").attr("class", "links");
    nodeGroup = container.append("g").attr("class", "nodes");
    labelGroup = container.append("g").attr("class", "labels");

    // Force simulation
    simulation = d3
      .forceSimulation()
      .force(
        "link",
        d3
          .forceLink()
          .id(function (d) {
            return d.id;
          })
          .distance(120)
      )
      .force("charge", d3.forceManyBody().strength(-300))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .force("collision", d3.forceCollide().radius(NODE_RADIUS + 8))
      .on("tick", ticked);

    // Load initial data
    var initial = window.__TOPOLOGY_DATA__ || { nodes: [], links: [] };
    update(initial);

    // Wire up buttons
    bindButtons();

    // Handle window resize
    window.addEventListener("resize", onResize);
  }

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  function update(data) {
    currentData = data;

    // Links
    var links = linkGroup
      .selectAll("line")
      .data(data.links, function (d) {
        return d.source.id || d.source + "-" + (d.target.id || d.target) + "-" + d.type;
      });

    links.exit().remove();

    var linksEnter = links
      .enter()
      .append("line")
      .attr("stroke", "#d1d5db")
      .attr("stroke-width", 1.5)
      .attr("marker-end", "url(#arrowhead)");

    links = linksEnter.merge(links);

    // Link labels
    var linkLabels = linkGroup
      .selectAll("text")
      .data(data.links, function (d) {
        return (d.source.id || d.source) + "-" + (d.target.id || d.target) + "-" + d.type;
      });

    linkLabels.exit().remove();

    var linkLabelsEnter = linkLabels
      .enter()
      .append("text")
      .attr("text-anchor", "middle")
      .attr("font-size", "9px")
      .attr("fill", "#9ca3af")
      .attr("dy", -6)
      .text(function (d) {
        return d.type;
      });

    linkLabels = linkLabelsEnter.merge(linkLabels);

    // Nodes
    var nodes = nodeGroup
      .selectAll("g")
      .data(data.nodes, function (d) {
        return d.id;
      });

    nodes.exit().remove();

    var nodesEnter = nodes.enter().append("g").attr("class", "node-group");

    // Main circle
    nodesEnter
      .append("circle")
      .attr("r", NODE_RADIUS)
      .attr("fill", nodeColor)
      .attr("stroke", function (d) {
        return d.manually_edited ? "#eab308" : "#e5e7eb";
      })
      .attr("stroke-width", function (d) {
        return d.manually_edited ? 3 : 2;
      })
      .attr("stroke-dasharray", function (d) {
        return d.manually_edited ? "4,3" : "none";
      })
      .attr("cursor", "pointer")
      .on("click", function (event, d) {
        event.stopPropagation();
        showNodeDetail(d);
        document.dispatchEvent(
          new CustomEvent("node-clicked", { detail: { entity_id: d.id } })
        );
      });

    // Source badge (manual vs auto)
    nodesEnter
      .append("text")
      .attr("class", "source-badge")
      .attr("text-anchor", "middle")
      .attr("dy", NODE_RADIUS + 20)
      .attr("font-size", "8px")
      .attr("fill", function (d) {
        return d.manually_edited ? "#eab308" : "#9ca3af";
      })
      .text(function (d) {
        return d.manually_edited ? "manual" : "auto";
      });

    // Drag behavior
    nodesEnter.call(
      d3
        .drag()
        .on("start", dragStarted)
        .on("drag", dragged)
        .on("end", dragEnded)
    );

    nodes = nodesEnter.merge(nodes);

    // Update existing node visuals
    nodes
      .select("circle")
      .attr("fill", nodeColor)
      .attr("stroke", function (d) {
        return d.manually_edited ? "#eab308" : "#e5e7eb";
      })
      .attr("stroke-width", function (d) {
        return d.manually_edited ? 3 : 2;
      })
      .attr("stroke-dasharray", function (d) {
        return d.manually_edited ? "4,3" : "none";
      });

    // Labels
    var labels = labelGroup
      .selectAll("text")
      .data(data.nodes, function (d) {
        return d.id;
      });

    labels.exit().remove();

    var labelsEnter = labels
      .enter()
      .append("text")
      .attr("text-anchor", "middle")
      .attr("font-size", "11px")
      .attr("font-weight", "500")
      .attr("fill", "#374151")
      .attr("pointer-events", "none")
      .text(function (d) {
        return d.name;
      });

    labels = labelsEnter.merge(labels);

    // Restart simulation
    simulation.nodes(data.nodes);
    simulation.force("link").links(data.links);
    simulation.alpha(0.8).restart();

    // Store selections for tick
    svg.__links = linkGroup.selectAll("line");
    svg.__linkLabels = linkGroup.selectAll("text");
    svg.__nodes = nodeGroup.selectAll("g");
    svg.__labels = labelGroup.selectAll("text");
  }

  function ticked() {
    if (!svg.__links) return;

    svg.__links
      .attr("x1", function (d) { return d.source.x; })
      .attr("y1", function (d) { return d.source.y; })
      .attr("x2", function (d) { return d.target.x; })
      .attr("y2", function (d) { return d.target.y; });

    svg.__linkLabels
      .attr("x", function (d) { return (d.source.x + d.target.x) / 2; })
      .attr("y", function (d) { return (d.source.y + d.target.y) / 2; });

    svg.__nodes.attr("transform", function (d) {
      return "translate(" + d.x + "," + d.y + ")";
    });

    svg.__labels
      .attr("x", function (d) { return d.x; })
      .attr("y", function (d) { return d.y - LABEL_OFFSET; });
  }

  // -----------------------------------------------------------------------
  // Drag handlers
  // -----------------------------------------------------------------------

  function dragStarted(event, d) {
    if (!event.active) simulation.alphaTarget(0.3).restart();
    d.fx = d.x;
    d.fy = d.y;
  }

  function dragged(event, d) {
    d.fx = event.x;
    d.fy = event.y;
  }

  function dragEnded(event, d) {
    if (!event.active) simulation.alphaTarget(0);
    d.fx = null;
    d.fy = null;
  }

  // -----------------------------------------------------------------------
  // Side panel — node detail
  // -----------------------------------------------------------------------

  function showNodeDetail(d) {
    var panel = document.getElementById("side-panel");
    var content = document.getElementById("side-panel-content");
    if (!panel || !content) return;

    panel.classList.remove("hidden");

    var canEdit =
      document.getElementById("btn-add-node") !== null;

    var html =
      '<div class="flex items-center justify-between mb-4">' +
      '  <h2 class="text-sm font-bold text-gray-900">' + escapeHtml(d.name) + "</h2>" +
      '  <button id="btn-close-panel" class="text-gray-400 hover:text-gray-600 text-lg">&times;</button>' +
      "</div>" +
      '<dl class="space-y-2 text-sm">' +
      "  <div><dt class='text-xs text-gray-500'>Entity ID</dt><dd class='font-mono text-gray-700'>" +
      escapeHtml(d.id) + "</dd></div>" +
      "  <div><dt class='text-xs text-gray-500'>Type</dt><dd>" +
      '<span class="inline-block w-2 h-2 rounded-full mr-1" style="background:' + nodeColor(d) + '"></span>' +
      escapeHtml(d.type) + "</dd></div>" +
      "  <div><dt class='text-xs text-gray-500'>IP Address</dt><dd>" +
      escapeHtml(d.ip || "N/A") + "</dd></div>" +
      "  <div><dt class='text-xs text-gray-500'>Source</dt><dd>" +
      escapeHtml(d.source || "unknown") + "</dd></div>" +
      "  <div><dt class='text-xs text-gray-500'>Origin</dt><dd>" +
      (d.manually_edited
        ? '<span class="text-yellow-600">Manual</span>'
        : '<span class="text-gray-500">Auto-discovered</span>') +
      "</dd></div>" +
      "</dl>";

    if (canEdit) {
      html +=
        '<div class="mt-4 pt-4 border-t border-gray-200 flex gap-2">' +
        '  <button id="btn-edit-node" data-id="' + escapeHtml(d.id) + '"' +
        '    class="bg-gray-100 text-gray-600 px-3 py-1 rounded text-xs hover:bg-gray-200">Edit</button>' +
        '  <button id="btn-delete-node" data-id="' + escapeHtml(d.id) + '"' +
        '    class="bg-red-50 text-red-600 px-3 py-1 rounded text-xs hover:bg-red-100">Delete</button>' +
        "</div>";
    }

    content.innerHTML = html;

    // Close button
    var closeBtn = document.getElementById("btn-close-panel");
    if (closeBtn) {
      closeBtn.addEventListener("click", function () {
        panel.classList.add("hidden");
      });
    }

    // Delete button
    var deleteBtn = document.getElementById("btn-delete-node");
    if (deleteBtn) {
      deleteBtn.addEventListener("click", function () {
        var entityId = this.getAttribute("data-id");
        if (confirm("Delete node " + entityId + " and all its edges?")) {
          apiRequest("DELETE", "/ui/service-map/nodes/" + encodeURIComponent(entityId))
            .then(function () {
              panel.classList.add("hidden");
              refreshGraph();
              showToast("success", "Node deleted");
            })
            .catch(function (err) {
              showToast("error", "Delete failed: " + err.message);
            });
        }
      });
    }

    // Edit button
    var editBtn = document.getElementById("btn-edit-node");
    if (editBtn) {
      editBtn.addEventListener("click", function () {
        var entityId = this.getAttribute("data-id");
        showEditForm(d, entityId);
      });
    }
  }

  function showEditForm(d, entityId) {
    var content = document.getElementById("side-panel-content");
    if (!content) return;

    content.innerHTML =
      '<h2 class="text-sm font-bold text-gray-900 mb-4">Edit Node</h2>' +
      '<form id="edit-node-form" class="space-y-3">' +
      "  <div>" +
      '    <label class="block text-xs font-medium text-gray-500 mb-1">Display Name</label>' +
      '    <input name="display_name" type="text" value="' + escapeHtml(d.name) + '"' +
      '      class="block w-full rounded border-gray-300 text-sm py-1.5 px-2">' +
      "  </div>" +
      "  <div>" +
      '    <label class="block text-xs font-medium text-gray-500 mb-1">Type</label>' +
      '    <select name="entity_type" class="block w-full rounded border-gray-300 text-sm py-1.5 px-2">' +
      '      <option value="service"' + (d.type === "service" ? " selected" : "") + ">Service</option>" +
      '      <option value="host"' + (d.type === "host" ? " selected" : "") + ">Host</option>" +
      '      <option value="network_device"' + (d.type === "network_device" ? " selected" : "") + ">Network Device</option>" +
      '      <option value="proxy"' + (d.type === "proxy" ? " selected" : "") + ">Proxy</option>" +
      '      <option value="monitor"' + (d.type === "monitor" ? " selected" : "") + ">Monitor</option>" +
      "    </select>" +
      "  </div>" +
      "  <div>" +
      '    <label class="block text-xs font-medium text-gray-500 mb-1">Host IP</label>' +
      '    <input name="host_ip" type="text" value="' + escapeHtml(d.ip || "") + '"' +
      '      class="block w-full rounded border-gray-300 text-sm py-1.5 px-2">' +
      "  </div>" +
      '  <div class="flex justify-end gap-2 pt-2">' +
      '    <button type="button" id="btn-cancel-edit"' +
      '      class="bg-gray-100 text-gray-600 px-3 py-1 rounded text-xs hover:bg-gray-200">Cancel</button>' +
      '    <button type="submit"' +
      '      class="bg-blue-600 text-white px-3 py-1 rounded text-xs hover:bg-blue-700">Save</button>' +
      "  </div>" +
      "</form>";

    document.getElementById("btn-cancel-edit").addEventListener("click", function () {
      showNodeDetail(d);
    });

    document.getElementById("edit-node-form").addEventListener("submit", function (e) {
      e.preventDefault();
      var form = new FormData(this);
      var payload = {
        display_name: form.get("display_name"),
        entity_type: form.get("entity_type"),
        host_ip: form.get("host_ip") || null,
      };
      apiRequest("PUT", "/ui/service-map/nodes/" + encodeURIComponent(entityId), payload)
        .then(function () {
          refreshGraph();
          showToast("success", "Node updated");
          // Close panel
          document.getElementById("side-panel").classList.add("hidden");
        })
        .catch(function (err) {
          showToast("error", "Update failed: " + err.message);
        });
    });
  }

  // -----------------------------------------------------------------------
  // Button bindings
  // -----------------------------------------------------------------------

  function bindButtons() {
    // Add Node
    var addBtn = document.getElementById("btn-add-node");
    if (addBtn) {
      addBtn.addEventListener("click", function () {
        window.dispatchEvent(new CustomEvent("open-add-node"));
      });
    }

    // Add Node form submit
    var addForm = document.getElementById("add-node-form");
    if (addForm) {
      addForm.addEventListener("submit", function (e) {
        e.preventDefault();
        var fd = new FormData(this);
        var payload = {
          entity_id: fd.get("entity_id"),
          display_name: fd.get("display_name") || fd.get("entity_id"),
          entity_type: fd.get("entity_type"),
          host_ip: fd.get("host_ip") || null,
        };
        apiRequest("POST", "/ui/service-map/nodes", payload)
          .then(function () {
            addForm.reset();
            // Close modal via Alpine
            window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
            refreshGraph();
            showToast("success", "Node created");
          })
          .catch(function (err) {
            showToast("error", "Create failed: " + err.message);
          });
      });
    }

    // Re-discover
    var discoverBtn = document.getElementById("btn-discover");
    if (discoverBtn) {
      discoverBtn.addEventListener("click", function () {
        discoverBtn.disabled = true;
        discoverBtn.textContent = "Discovering...";
        apiRequest("POST", "/ui/service-map/discover")
          .then(function (result) {
            refreshGraph();
            var msg =
              "Found " + result.nodes_discovered + " nodes, " +
              result.edges_discovered + " edges" +
              (result.changes > 0 ? " (" + result.changes + " changes)" : "");
            showToast("success", msg);
          })
          .catch(function (err) {
            showToast("error", "Discovery failed: " + err.message);
          })
          .finally(function () {
            discoverBtn.disabled = false;
            discoverBtn.textContent = "Re-discover";
          });
      });
    }

    // Fit View
    var fitBtn = document.getElementById("btn-fit");
    if (fitBtn) {
      fitBtn.addEventListener("click", fitView);
    }

    // Click background to close panel
    svg.on("click", function () {
      var panel = document.getElementById("side-panel");
      if (panel) panel.classList.add("hidden");
    });
  }

  // -----------------------------------------------------------------------
  // Utilities
  // -----------------------------------------------------------------------

  function refreshGraph() {
    fetch("/ui/service-map/data", {
      headers: { "X-CSRF-Token": getCsrfToken() },
    })
      .then(function (resp) {
        return resp.json();
      })
      .then(function (data) {
        update(data);
      })
      .catch(function (err) {
        console.error("Failed to refresh graph:", err);
      });
  }

  function fitView() {
    if (!currentData.nodes.length) return;

    var bounds = container.node().getBBox();
    if (bounds.width === 0 || bounds.height === 0) return;

    var padding = 40;
    var fullWidth = svg.node().parentElement.getBoundingClientRect().width;
    var fullHeight = svg.node().parentElement.getBoundingClientRect().height;

    var scale = Math.min(
      (fullWidth - padding * 2) / bounds.width,
      (fullHeight - padding * 2) / bounds.height,
      2 // max zoom
    );

    var tx = fullWidth / 2 - scale * (bounds.x + bounds.width / 2);
    var ty = fullHeight / 2 - scale * (bounds.y + bounds.height / 2);

    svg
      .transition()
      .duration(500)
      .call(
        svg.__zoom_behavior.transform,
        d3.zoomIdentity.translate(tx, ty).scale(scale)
      );
  }

  function onResize() {
    var rect = svg.node().parentElement.getBoundingClientRect();
    width = rect.width;
    height = rect.height;
    svg.attr("viewBox", [0, 0, width, height]);
    simulation.force("center", d3.forceCenter(width / 2, height / 2));
    simulation.alpha(0.3).restart();
  }

  function escapeHtml(str) {
    if (!str) return "";
    var div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function showToast(type, message) {
    window.dispatchEvent(
      new CustomEvent("toast-" + type, { detail: { message: message } })
    );
  }

  // -----------------------------------------------------------------------
  // Boot
  // -----------------------------------------------------------------------

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
