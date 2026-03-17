/**
 * OasisAgent Service Map — D3 force-directed topology graph.
 *
 * Loads topology from window.__TOPOLOGY_DATA__ (server-rendered)
 * or fetches from /ui/service-map/data.
 *
 * Node colors by entity_type:
 *   network_device = blue, service = green, host = gray,
 *   proxy = orange, monitor = purple, stack = teal (collapsed)
 *
 * Stack nodes are collapsible — collapsed by default, click to
 * expand and show member containers.
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
    container: "#06b6d4",
    stack: "#14b8a6",
    external: "#ef4444",
  };

  var EDGE_COLORS = {
    runs_on: "#9ca3af",
    depends_on: "#3b82f6",
    proxies_to: "#f97316",
    forwards_to: "#8b5cf6",
    resolves_via: "#06b6d4",
    connects_via: "#6b7280",
    member_of: "#14b8a6",
    hosted_by: "#6b7280",
  };

  var DEFAULT_COLOR = "#94a3b8";
  var DEFAULT_EDGE_COLOR = "#d1d5db";
  var NODE_RADIUS = 24;
  var STACK_RADIUS = 34;
  var LABEL_OFFSET = NODE_RADIUS + 10;
  var STACK_LABEL_OFFSET = STACK_RADIUS + 10;
  var ARROW_SIZE = 8;

  // -----------------------------------------------------------------------
  // State
  // -----------------------------------------------------------------------

  var svg, graphContainer, simulation;
  var linkGroup, nodeGroup, labelGroup;
  var width, height;
  var rawData = { nodes: [], links: [] };
  var currentData = { nodes: [], links: [] };

  // Stacks collapsed by default — set of stack entity_ids
  var collapsedStacks = {};

  var filterState = {
    searchText: "",
    typeFilters: {
      network_device: true, service: true, host: true,
      container: true, proxy: true, monitor: true,
      stack: true, external: true,
    },
    focusNode: null,
    focusDepth: 1,
  };
  var searchDebounceTimer = null;

  // -----------------------------------------------------------------------
  // Helpers
  // -----------------------------------------------------------------------

  function nodeColor(d) {
    return TYPE_COLORS[d.type] || DEFAULT_COLOR;
  }

  function nodeRadius(d) {
    return d.type === "stack" ? STACK_RADIUS : NODE_RADIUS;
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
  // Stack collapse logic
  // -----------------------------------------------------------------------

  /**
   * Build the mapping: stackId → [member entity_ids].
   * Also returns memberToStack: memberId → stackId.
   */
  function buildStackMembership(data) {
    var stackMembers = {};   // stackId → [memberIds]
    var memberToStack = {};  // memberId → stackId

    for (var i = 0; i < data.links.length; i++) {
      var link = data.links[i];
      var srcId = link.source.id || link.source;
      var tgtId = link.target.id || link.target;
      if (link.type === "member_of") {
        // from=container, to=stack
        if (!stackMembers[tgtId]) stackMembers[tgtId] = [];
        stackMembers[tgtId].push(srcId);
        memberToStack[srcId] = tgtId;
      }
    }
    return { stackMembers: stackMembers, memberToStack: memberToStack };
  }

  /**
   * Process raw data with stack collapse applied.
   * Returns a new {nodes, links} with collapsed stacks' members hidden.
   */
  function applyCollapse(data) {
    var membership = buildStackMembership(data);
    var stackMembers = membership.stackMembers;
    var memberToStack = membership.memberToStack;

    // Build set of hidden node IDs (members of collapsed stacks)
    var hiddenNodes = {};
    var stackNodeIds = {};
    for (var i = 0; i < data.nodes.length; i++) {
      if (data.nodes[i].type === "stack") {
        stackNodeIds[data.nodes[i].id] = true;
      }
    }

    for (var stackId in collapsedStacks) {
      if (!collapsedStacks[stackId]) continue;
      var members = stackMembers[stackId] || [];
      for (var j = 0; j < members.length; j++) {
        hiddenNodes[members[j]] = true;
      }
    }

    // Filter nodes — keep non-hidden, annotate stacks with member count
    var filteredNodes = [];
    for (var k = 0; k < data.nodes.length; k++) {
      var node = data.nodes[k];
      if (hiddenNodes[node.id]) continue;

      // Clone and annotate stack nodes
      if (node.type === "stack") {
        var count = (stackMembers[node.id] || []).length;
        var clone = Object.assign({}, node);
        clone.memberCount = count;
        clone.collapsed = !!collapsedStacks[node.id];
        filteredNodes.push(clone);
      } else {
        filteredNodes.push(node);
      }
    }

    // Filter links — hide if either endpoint is hidden, or if it's a
    // member_of edge to a collapsed stack
    var filteredLinks = [];
    for (var m = 0; m < data.links.length; m++) {
      var link = data.links[m];
      var srcId = link.source.id || link.source;
      var tgtId = link.target.id || link.target;

      if (hiddenNodes[srcId] || hiddenNodes[tgtId]) continue;
      // Hide member_of edges when stack is collapsed (the stack node
      // itself represents the relationship)
      if (link.type === "member_of" && collapsedStacks[tgtId]) continue;

      filteredLinks.push(link);
    }

    return { nodes: filteredNodes, links: filteredLinks };
  }

  /**
   * Initialize all stacks as collapsed.
   */
  function initCollapsedStacks(data) {
    collapsedStacks = {};
    for (var i = 0; i < data.nodes.length; i++) {
      if (data.nodes[i].type === "stack") {
        collapsedStacks[data.nodes[i].id] = true;
      }
    }
  }

  function toggleStack(stackId) {
    collapsedStacks[stackId] = !collapsedStacks[stackId];
    var processed = applyCollapse(rawData);
    update(processed, true);
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
        graphContainer.attr("transform", event.transform);
      });

    svg.call(zoom);

    // Store zoom for fitView
    svg.__zoom_behavior = zoom;

    // Container for zoomable content
    graphContainer = svg.append("g").attr("class", "graph-container");

    // Draw order: links, nodes, labels
    linkGroup = graphContainer.append("g").attr("class", "links");
    nodeGroup = graphContainer.append("g").attr("class", "nodes");
    labelGroup = graphContainer.append("g").attr("class", "labels");

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
          .distance(function (d) {
            // Shorter distance for member_of edges within a stack
            if (d.type === "member_of") return 60;
            return 120;
          })
      )
      .force("charge", d3.forceManyBody().strength(-300))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .force("collision", d3.forceCollide().radius(function (d) {
        return nodeRadius(d) + 8;
      }))
      .on("tick", ticked);

    // Load initial data
    var initial = window.__TOPOLOGY_DATA__ || { nodes: [], links: [] };
    rawData = initial;
    initCollapsedStacks(initial);
    var processed = applyCollapse(initial);
    update(processed, false);

    // Auto-fit after simulation stabilizes
    simulation.on("end", function onceStabilized() {
      fitView();
      simulation.on("end", null); // Only run once
    });

    // Wire up buttons
    bindButtons();

    // Handle window resize
    window.addEventListener("resize", onResize);
  }

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  function update(data, preservePositions) {
    currentData = data;

    // Build position map from existing nodes to preserve layout
    var posMap = {};
    if (preservePositions && simulation) {
      var oldNodes = simulation.nodes();
      for (var p = 0; p < oldNodes.length; p++) {
        posMap[oldNodes[p].id] = { x: oldNodes[p].x, y: oldNodes[p].y };
      }
      // Apply saved positions to new nodes
      for (var q = 0; q < data.nodes.length; q++) {
        var saved = posMap[data.nodes[q].id];
        if (saved) {
          data.nodes[q].x = saved.x;
          data.nodes[q].y = saved.y;
        }
      }
    }

    // Links
    var links = linkGroup
      .selectAll("line")
      .data(data.links, function (d) {
        return (d.source.id || d.source) + "-" + (d.target.id || d.target) + "-" + d.type;
      });

    links.exit().remove();

    var linksEnter = links
      .enter()
      .append("line")
      .attr("stroke", function (d) { return EDGE_COLORS[d.type] || DEFAULT_EDGE_COLOR; })
      .attr("stroke-width", 1.5)
      .attr("stroke-dasharray", function (d) { return d.manually_edited ? "6,3" : "none"; })
      .attr("marker-end", "url(#arrowhead)");

    links = linksEnter.merge(links);

    // Update existing edge visuals
    links
      .attr("stroke", function (d) { return EDGE_COLORS[d.type] || DEFAULT_EDGE_COLOR; })
      .attr("stroke-dasharray", function (d) { return d.manually_edited ? "6,3" : "none"; });

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
      .attr("fill", function (d) { return EDGE_COLORS[d.type] || "#9ca3af"; })
      .attr("dy", -6)
      .attr("display", "none")
      .text(function (d) {
        return d.type;
      });

    linkLabels = linkLabelsEnter.merge(linkLabels);

    // Update existing link label colors
    linkLabels.attr("fill", function (d) { return EDGE_COLORS[d.type] || "#9ca3af"; });

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
      .attr("r", nodeRadius)
      .attr("fill", nodeColor)
      .attr("stroke", function (d) {
        if (d.type === "stack") return d.collapsed ? "#0d9488" : "#5eead4";
        return d.manually_edited ? "#eab308" : "#e5e7eb";
      })
      .attr("stroke-width", function (d) {
        if (d.type === "stack") return 3;
        return d.manually_edited ? 3 : 2;
      })
      .attr("stroke-dasharray", function (d) {
        return d.manually_edited ? "4,3" : "none";
      })
      .attr("cursor", "pointer")
      .on("click", function (event, d) {
        event.stopPropagation();
        if (d.type === "stack") {
          toggleStack(d.id);
        } else {
          showNodeDetail(d);
          document.dispatchEvent(
            new CustomEvent("node-clicked", { detail: { entity_id: d.id } })
          );
        }
      });

    // Count badge for stack nodes (inside circle)
    nodesEnter
      .filter(function (d) { return d.type === "stack"; })
      .append("text")
      .attr("class", "stack-count")
      .attr("text-anchor", "middle")
      .attr("dy", "0.35em")
      .attr("font-size", "14px")
      .attr("font-weight", "700")
      .attr("fill", "#ffffff")
      .attr("pointer-events", "none")
      .text(function (d) { return d.memberCount || ""; });

    // Source badge — only for manually edited nodes
    nodesEnter
      .filter(function (d) { return d.type !== "stack" && d.manually_edited; })
      .append("text")
      .attr("class", "source-badge")
      .attr("text-anchor", "middle")
      .attr("dy", NODE_RADIUS + 20)
      .attr("font-size", "8px")
      .attr("fill", "#eab308")
      .text("manual");

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
      .attr("r", nodeRadius)
      .attr("fill", nodeColor)
      .attr("stroke", function (d) {
        if (d.type === "stack") return d.collapsed ? "#0d9488" : "#5eead4";
        return d.manually_edited ? "#eab308" : "#e5e7eb";
      })
      .attr("stroke-width", function (d) {
        if (d.type === "stack") return 3;
        return d.manually_edited ? 3 : 2;
      })
      .attr("stroke-dasharray", function (d) {
        return d.manually_edited ? "4,3" : "none";
      });

    // Update stack count text
    nodes
      .select(".stack-count")
      .text(function (d) { return d.memberCount || ""; });

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
      .attr("font-size", function (d) { return d.type === "stack" ? "12px" : "11px"; })
      .attr("font-weight", function (d) { return d.type === "stack" ? "700" : "500"; })
      .attr("fill", "#374151")
      .attr("pointer-events", "none")
      .text(function (d) {
        if (d.type === "stack" && d.collapsed) {
          return d.name + " (" + (d.memberCount || 0) + ")";
        }
        return d.name;
      });

    labels = labelsEnter.merge(labels);

    // Update label text for stacks (may change on collapse toggle)
    labels.text(function (d) {
      if (d.type === "stack" && d.collapsed) {
        return d.name + " (" + (d.memberCount || 0) + ")";
      }
      return d.name;
    });

    // Restart simulation
    simulation.nodes(data.nodes);
    simulation.force("link").links(data.links);
    simulation.alpha(preservePositions ? 0.3 : 0.8).restart();

    // Store selections for tick
    svg.__links = linkGroup.selectAll("line");
    svg.__linkLabels = linkGroup.selectAll("text");
    svg.__nodes = nodeGroup.selectAll("g");
    svg.__labels = labelGroup.selectAll("text");

    // Apply current filters and rebuild edge datalists
    applyFilters();
    rebuildEdgeDatalist();
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
      .attr("y", function (d) {
        return d.y - (d.type === "stack" ? STACK_LABEL_OFFSET : LABEL_OFFSET);
      });
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

    // Focus button (all roles)
    html +=
      '<div class="mt-4 pt-4 border-t border-gray-200 flex flex-wrap gap-2">' +
      '  <button id="btn-focus-node" data-id="' + escapeHtml(d.id) + '"' +
      '    class="bg-indigo-50 text-indigo-600 px-3 py-1 rounded text-xs hover:bg-indigo-100">Focus</button>';

    if (canEdit) {
      html +=
        '  <button id="btn-connect-node" data-id="' + escapeHtml(d.id) + '"' +
        '    class="bg-green-50 text-green-600 px-3 py-1 rounded text-xs hover:bg-green-100">Connect</button>' +
        '  <button id="btn-edit-node" data-id="' + escapeHtml(d.id) + '"' +
        '    class="bg-gray-100 text-gray-600 px-3 py-1 rounded text-xs hover:bg-gray-200">Edit</button>' +
        '  <button id="btn-delete-node" data-id="' + escapeHtml(d.id) + '"' +
        '    class="bg-red-50 text-red-600 px-3 py-1 rounded text-xs hover:bg-red-100">Delete</button>';
    }

    html += "</div>";

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

    // Focus button
    var focusBtn = document.getElementById("btn-focus-node");
    if (focusBtn) {
      focusBtn.addEventListener("click", function () {
        filterState.focusNode = this.getAttribute("data-id");
        filterState.focusDepth = 1;
        applyFilters();
      });
    }

    // Connect button — pre-fill Add Edge modal
    var connectBtn = document.getElementById("btn-connect-node");
    if (connectBtn) {
      connectBtn.addEventListener("click", function () {
        var fromInput = document.getElementById("edge-from-entity");
        if (fromInput) fromInput.value = this.getAttribute("data-id");
        window.dispatchEvent(new CustomEvent("open-add-edge"));
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
      '      <option value="container"' + (d.type === "container" ? " selected" : "") + ">Container</option>" +
      '      <option value="stack"' + (d.type === "stack" ? " selected" : "") + ">Stack</option>" +
      '      <option value="network_device"' + (d.type === "network_device" ? " selected" : "") + ">Network Device</option>" +
      '      <option value="proxy"' + (d.type === "proxy" ? " selected" : "") + ">Proxy</option>" +
      '      <option value="monitor"' + (d.type === "monitor" ? " selected" : "") + ">Monitor</option>" +
      '      <option value="external"' + (d.type === "external" ? " selected" : "") + ">External</option>" +
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

    // Add Edge
    var addEdgeBtn = document.getElementById("btn-add-edge");
    if (addEdgeBtn) {
      addEdgeBtn.addEventListener("click", function () {
        window.dispatchEvent(new CustomEvent("open-add-edge"));
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

    // Add Edge form submit
    var addEdgeForm = document.getElementById("add-edge-form");
    if (addEdgeForm) {
      addEdgeForm.addEventListener("submit", function (e) {
        e.preventDefault();
        var fd = new FormData(this);
        var payload = {
          from_entity: fd.get("from_entity"),
          to_entity: fd.get("to_entity"),
          edge_type: fd.get("edge_type"),
        };
        apiRequest("POST", "/ui/service-map/edges", payload)
          .then(function () {
            addEdgeForm.reset();
            window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
            refreshGraph();
            showToast("success", "Edge created");
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

    // --- Filter controls ---

    // Search input
    var searchInput = document.getElementById("filter-search");
    if (searchInput) {
      searchInput.addEventListener("input", function () {
        var val = this.value;
        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = setTimeout(function () {
          filterState.searchText = val;
          applyFilters();
        }, 150);
      });
      var clearBtn = document.getElementById("filter-search-clear");
      if (clearBtn) {
        clearBtn.addEventListener("click", function () {
          searchInput.value = "";
          filterState.searchText = "";
          applyFilters();
        });
      }
    }

    // Type filter pills
    initTypeFilterPills();

    // Focus depth controls
    var depthMinus = document.getElementById("focus-depth-minus");
    var depthPlus = document.getElementById("focus-depth-plus");
    if (depthMinus) {
      depthMinus.addEventListener("click", function () {
        if (filterState.focusDepth > 1) {
          filterState.focusDepth--;
          try { applyFilters(); } catch (e) { updateFocusIndicator(); }
        }
      });
    }
    if (depthPlus) {
      depthPlus.addEventListener("click", function () {
        if (filterState.focusDepth < 5) {
          filterState.focusDepth++;
          try { applyFilters(); } catch (e) { updateFocusIndicator(); }
        }
      });
    }

    // Show All (exit focus)
    var showAllBtn = document.getElementById("focus-show-all");
    if (showAllBtn) {
      showAllBtn.addEventListener("click", function () {
        filterState.focusNode = null;
        filterState.focusDepth = 1;
        applyFilters();
      });
    }

    // Click background to close panel (NOT exit focus — that's explicit via Show All)
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
        rawData = data;
        initCollapsedStacks(data);
        var processed = applyCollapse(data);
        update(processed, false);
      })
      .catch(function (err) {
        console.error("Failed to refresh graph:", err);
      });
  }

  function fitView() {
    if (!currentData.nodes.length) return;

    var bounds = graphContainer.node().getBBox();
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
  // Filter & Focus
  // -----------------------------------------------------------------------

  function getNeighborhood(nodeId, depth) {
    var visited = {};
    visited[nodeId] = true;
    var frontier = [nodeId];

    for (var d = 0; d < depth; d++) {
      var nextFrontier = [];
      for (var i = 0; i < frontier.length; i++) {
        var nid = frontier[i];
        for (var j = 0; j < currentData.links.length; j++) {
          var link = currentData.links[j];
          var srcId = link.source.id || link.source;
          var tgtId = link.target.id || link.target;
          if (srcId === nid && !visited[tgtId]) {
            visited[tgtId] = true;
            nextFrontier.push(tgtId);
          } else if (tgtId === nid && !visited[srcId]) {
            visited[srcId] = true;
            nextFrontier.push(srcId);
          }
        }
      }
      frontier = nextFrontier;
    }
    return visited;
  }

  function applyFilters() {
    if (!svg || !svg.__nodes) return;

    // If focus node was deleted, clear focus
    if (filterState.focusNode) {
      var found = false;
      for (var i = 0; i < currentData.nodes.length; i++) {
        if (currentData.nodes[i].id === filterState.focusNode) { found = true; break; }
      }
      if (!found) {
        filterState.focusNode = null;
        filterState.focusDepth = 1;
      }
    }

    var neighborhood = filterState.focusNode
      ? getNeighborhood(filterState.focusNode, filterState.focusDepth)
      : null;

    var searchLower = filterState.searchText.toLowerCase();

    // Build set of visible node IDs for edge filtering
    var visibleNodes = {};

    svg.__nodes.each(function (d) {
      var el = d3.select(this);
      var typeHidden = !filterState.typeFilters[d.type];
      var focusHidden = neighborhood && !neighborhood[d.id];
      var hidden = typeHidden || focusHidden;

      el.attr("display", hidden ? "none" : null);

      if (!hidden) {
        visibleNodes[d.id] = true;
        if (searchLower && d.name) {
          var matches = d.name.toLowerCase().indexOf(searchLower) !== -1 ||
            d.id.toLowerCase().indexOf(searchLower) !== -1;
          el.attr("opacity", matches ? 1 : 0.15);
          // Gold highlight ring on matches
          el.select("circle")
            .attr("stroke", matches && searchLower ? "#eab308" :
              (d.manually_edited ? "#eab308" : "#e5e7eb"))
            .attr("stroke-width", matches && searchLower ? 3 :
              (d.manually_edited ? 3 : 2));
        } else {
          el.attr("opacity", 1);
          el.select("circle")
            .attr("stroke", function () {
              if (d.type === "stack") return d.collapsed ? "#0d9488" : "#5eead4";
              return d.manually_edited ? "#eab308" : "#e5e7eb";
            })
            .attr("stroke-width", function () {
              if (d.type === "stack") return 3;
              return d.manually_edited ? 3 : 2;
            });
        }
      }
    });

    // Labels follow same visibility/opacity as nodes
    svg.__labels.each(function (d) {
      var el = d3.select(this);
      var typeHidden = !filterState.typeFilters[d.type];
      var focusHidden = neighborhood && !neighborhood[d.id];
      var hidden = typeHidden || focusHidden;

      el.attr("display", hidden ? "none" : null);

      if (!hidden && searchLower && d.name) {
        var matches = d.name.toLowerCase().indexOf(searchLower) !== -1 ||
          d.id.toLowerCase().indexOf(searchLower) !== -1;
        el.attr("opacity", matches ? 1 : 0.15);
      } else if (!hidden) {
        el.attr("opacity", 1);
      }
    });

    // Links: hide if either endpoint hidden
    svg.__links.each(function (d) {
      var srcId = d.source.id || d.source;
      var tgtId = d.target.id || d.target;
      var hidden = !visibleNodes[srcId] || !visibleNodes[tgtId];
      d3.select(this).attr("display", hidden ? "none" : null);
    });

    svg.__linkLabels.each(function (d) {
      var srcId = d.source.id || d.source;
      var tgtId = d.target.id || d.target;
      var hidden = !visibleNodes[srcId] || !visibleNodes[tgtId];
      d3.select(this).attr("display", hidden ? "none" : null);
    });

    updateFocusIndicator();
  }

  function updateFocusIndicator() {
    var indicator = document.getElementById("focus-indicator");
    if (!indicator) return;

    if (filterState.focusNode) {
      var nodeName = filterState.focusNode;
      for (var i = 0; i < currentData.nodes.length; i++) {
        if (currentData.nodes[i].id === filterState.focusNode) {
          nodeName = currentData.nodes[i].name;
          break;
        }
      }
      indicator.classList.remove("hidden");
      var label = indicator.querySelector("#focus-node-label");
      var depthLabel = indicator.querySelector("#focus-depth-label");
      if (label) label.textContent = nodeName;
      if (depthLabel) depthLabel.textContent = filterState.focusDepth;
    } else {
      indicator.classList.add("hidden");
    }
  }

  function initTypeFilterPills() {
    var pillContainer = document.getElementById("type-filter-pills");
    if (!pillContainer) return;

    var types = Object.keys(TYPE_COLORS);
    for (var i = 0; i < types.length; i++) {
      (function (type) {
        var pill = document.createElement("button");
        pill.className = "filter-pill active";
        pill.setAttribute("data-type", type);
        pill.innerHTML =
          '<span class="filter-pill__dot" style="background:' + TYPE_COLORS[type] + '"></span>' +
          type.replace("_", " ");
        pill.addEventListener("click", function () {
          filterState.typeFilters[type] = !filterState.typeFilters[type];
          pill.classList.toggle("active", filterState.typeFilters[type]);
          applyFilters();
        });
        pillContainer.appendChild(pill);
      })(types[i]);
    }
  }

  function rebuildEdgeDatalist() {
    var fromList = document.getElementById("edge-from-options");
    var toList = document.getElementById("edge-to-options");
    if (!fromList || !toList) return;

    var html = "";
    for (var i = 0; i < currentData.nodes.length; i++) {
      var n = currentData.nodes[i];
      html += '<option value="' + escapeHtml(n.id) + '">' +
        escapeHtml(n.name) + " (" + escapeHtml(n.id) + ")</option>";
    }
    fromList.innerHTML = html;
    toList.innerHTML = html;
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
