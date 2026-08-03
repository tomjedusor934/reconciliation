<script setup lang="ts">
import { computed } from 'vue';
import { Bar } from 'vue-chartjs';
import {
  Chart as ChartJS,
  Title,
  Tooltip,
  Legend,
  BarElement,
  CategoryScale,
  LinearScale,
  type ChartData,
  type ChartOptions,
} from 'chart.js';


// Register required Chart.js components
ChartJS.register(Title, Tooltip, Legend, BarElement, CategoryScale, LinearScale);

export interface HistogramProps {
  labels: string[];
  datasets: {
    label: string;
    data: number[];
    backgroundColor?: string | string[];
    borderColor?: string | string[];
    borderWidth?: number;
  }[];
  title?: string;
  height?: number;
  options?: ChartOptions<'bar'>;
}

const props = withDefaults(defineProps<HistogramProps>(), {
  title: '',
  height: 300,
  options: undefined,
});

// Chart data - Reveals brand colors
const chartData = computed<ChartData<'bar'>>(() => ({
  labels: props.labels,
  datasets: props.datasets.map((dataset: HistogramProps['datasets'][0]) => ({
    label: dataset.label,
    data: dataset.data,
    backgroundColor: dataset.backgroundColor || 'rgba(59, 130, 246, 0.8)',
    borderColor: dataset.borderColor || 'rgba(59, 130, 246, 1)',
    borderWidth: dataset.borderWidth || 2,
  })),
}));

// Options du graphique
const chartOptions = computed<ChartOptions<'bar'>>(() => {
  const defaultOptions: ChartOptions<'bar'> = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        display: true,
        position: 'top',
      },
      title: {
        display: !!props.title,
        text: props.title,
      },
    },
    scales: {
      y: {
        beginAtZero: true,
      },
    },
  };

  // Merge default options with custom options
  return props.options ? { ...defaultOptions, ...props.options } : defaultOptions;
});
</script>

<template>
  <div class="w-full">
    <div :style="{ height: `${height}px` }">
      <Bar :data="chartData" :options="chartOptions" />
    </div>
  </div>
</template>
