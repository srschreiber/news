# Keyword search

Search across every event by curated **keywords** (extracted per event), with
date-range filtering, topic filtering, and importance sorting. This complements
the site-wide full-text search in the top bar.

<div id="news-search">
  <div class="ns-controls">
    <input type="search" id="ns-q" placeholder="keywords, e.g. postgres, phishing, generics" autocomplete="off">
    <select id="ns-topic"><option value="">all topics</option></select>
    <label>from <input type="date" id="ns-from"></label>
    <label>to <input type="date" id="ns-to"></label>
    <label>min <span class="imp imp-low" title="Importance/10" aria-label="Importance">&#x2605;</span>
      <select id="ns-imp">
        <option value="1">1</option><option value="2">2</option>
        <option value="3">3</option><option value="4">4</option>
        <option value="5">5</option><option value="6">6</option>
        <option value="7">7</option><option value="8">8</option>
        <option value="9">9</option><option value="10">10</option>
      </select>
    </label>
    <select id="ns-sort">
      <option value="relevance">sort: relevance</option>
      <option value="date">sort: newest</option>
      <option value="importance">sort: importance</option>
    </select>
  </div>
  <p id="ns-status" class="ns-status"></p>
  <ol id="ns-results" class="ns-results"></ol>
</div>

<link rel="stylesheet" href="../assets/search.css">
<script src="../assets/search.js" defer></script>
