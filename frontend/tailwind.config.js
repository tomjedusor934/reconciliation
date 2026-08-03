import forms from '@tailwindcss/forms'

/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{vue,js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Reveals Brand Colors
        'space-indigo': {
          DEFAULT: '#2B2D42',
          50: '#F5F5F7',
          100: '#E8E9EF',
          200: '#D0D1DE',
          300: '#B8BACD',
          500: '#2B2D42',
          600: '#242637',
          700: '#1D1F2D',
        },
        'tropical-mint': {
          DEFAULT: '#00E49A',
          50: '#E6FFF8',
          100: '#B3FFEB',
          200: '#80FFDE',
          300: '#4DFFD0',
          500: '#00E49A',
          600: '#00B377',
          700: '#008254',
        },
        'ocean-mist': {
          DEFAULT: '#00C8B1',
          50: '#E0F8F4',
          100: '#B3EFE8',
          200: '#80E6DC',
          300: '#4DDDD0',
          500: '#00C8B1',
          600: '#00B39F',
          700: '#009E8D',
        },
        'turquoise-surf': {
          DEFAULT: '#00A7CB',
          50: '#E0F5FB',
          100: '#B3E5F5',
          200: '#80D4ED',
          300: '#4DC3E6',
          500: '#00A7CB',
          600: '#0088A8',
          700: '#006985',
        },
        'bright-lemon': {
          DEFAULT: '#F8F32B',
          50: '#FFFDE6',
          100: '#FFFACC',
          200: '#FFF799',
          300: '#FFF466',
          500: '#F8F32B',
          600: '#D9D200',
          700: '#BAB100',
        },
        'lavender-blush': {
          DEFAULT: '#F7EBEC',
          50: '#FDF9F9',
          100: '#FBF3F4',
          200: '#F7E7E9',
          300: '#F3DBDE',
          500: '#F7EBEC',
          600: '#E8D4D7',
          700: '#D9BDBE',
        },
      },
      fontFamily: {
        'rubik': ['Rubik', 'sans-serif'],
        'inter': ['Inter', 'sans-serif'],
        'jakarta': ['Plus Jakarta Sans', 'sans-serif'],
      },
      animation: {
        'blob': 'blob 7s infinite',
        'float': 'float 6s ease-in-out infinite',
        'fadeIn': 'fadeIn 0.3s ease-in-out',
      },
      keyframes: {
        blob: {
          '0%': { transform: 'translate(0px, 0px) scale(1)' },
          '33%': { transform: 'translate(30px, -50px) scale(1.1)' },
          '66%': { transform: 'translate(-20px, 20px) scale(0.9)' },
          '100%': { transform: 'translate(0px, 0px) scale(1)' },
        },
        float: {
          '0%, 100%': { transform: 'translateY(0)' },
          '50%': { transform: 'translateY(-10px)' },
        },
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
      },
      boxShadow: {
        'soft': '0 2px 8px -2px rgba(0, 0, 0, 0.05), 0 4px 12px -4px rgba(0, 0, 0, 0.05)',
        'glow-violet': '0 0 20px rgba(139, 92, 246, 0.15)',
        'glow-indigo': '0 0 20px rgba(99, 102, 241, 0.15)',
      },
      backgroundImage: {
        'gradient-brand': 'linear-gradient(135deg, #f8f32b 0%, #00e49a 70%, #00a7cb 100%)',
      },
      filter: {
        'drop-shadow-lemon': 'drop-shadow(0 2px 4px rgba(248, 243, 43, 0.3))',
      }
    },
  },
  plugins: [
    forms,
  ],
}
