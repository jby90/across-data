<script setup lang="ts">
withDefaults(defineProps<{ name?: string }>(), { name: 'grid' })
const paths: Record<string, string> = {
  grid: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  book: 'M4 4h6c2 0 2 2 2 2s0-2 2-2h6v15h-6c-2 0-2 1-2 1s0-1-2-1H4z M12 6v14',
  check: 'M9 3H5v18h14V3h-4 M9 2h6v4H9z M8 13l3 3 5-6',
  graph:
    'M12 4v5 M5 18l5-5 M19 18l-5-5 M9 10a3 3 0 1 0 6 0a3 3 0 1 0-6 0 M2 20h6v-5H2z M16 20h6v-5h-6z M9 1h6v3H9z',
  flow: 'M4 3h6v6H4z M14 15h6v6h-6z M7 9v8h7 M10 6h7v9',
  globe: 'M21 12a9 9 0 1 0-18 0a9 9 0 1 0 18 0 M3 12h18 M12 3c5 5 5 13 0 18c-5-5-5-13 0-18',
  building: 'M4 21V3h12v18 M16 9h4v12 M2 21h20 M8 7h4 M8 11h4 M8 15h4 M8 21v-3h4v3',
  users:
    'M16 21v-3c0-3-3-5-6-5s-6 2-6 5v3 M6 6a4 4 0 1 0 8 0a4 4 0 1 0-8 0 M17 3c4 0 4 6 0 6 M19 13c3 1 3 3 3 7',
  arrow: 'M4 12h16 M14 6l6 6-6 6',
  lock: 'M6 10V7a6 6 0 0 1 12 0v3 M4 10h16v12H4z M12 14v4',
  mail: 'M3 5h18v14H3z M3 5l9 8 9-8',
  menu: 'M4 6h16 M4 12h16 M4 18h16',
  logout: 'M10 4H4v16h6 M11 12h11 M17 7l5 5-5 5',
  plus: 'M12 4v16 M4 12h16',
  search: 'M10 3a7 7 0 1 0 0 14a7 7 0 1 0 0-14 M15 15l6 6',
  file: 'M5 2h9l5 5v15H5z M14 2v6h5 M8 12h8 M8 16h8',
  shield: 'M12 2l9 4v6c0 5-9 10-9 10S3 17 3 12V6z M8 12l3 3 5-6',
}
</script>
<template>
  <svg
    class="ui-icon"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.6"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path :d="paths[name] || paths.grid" />
  </svg>
</template>
