import { 
  LayoutDashboard, 
  Users, 
  Shield, 
  BarChart3, 
  Settings,
  LucideIcon,
  Boxes,
  Component,
  Workflow,
  ListChecks,
  GitMerge,
  PlayCircle,
  ScrollText,
  Archive,
} from 'lucide-vue-next'

/**
 * Interface pour un lien individuel dans la sidebar
 */
export interface SidebarLink {
  /** Label affiché du lien */
  label: string
  /** Route Vue Router vers laquelle naviguer */
  to: string
  /** Icône Lucide Vue (composant) */
  icon: typeof LucideIcon
  /** Chemin pour vérifier la permission via RBAC */
  permission: string
}

/**
 * Interface pour un groupe de liens
 */
export interface SidebarGroup {
  /** ID unique du groupe (pour le state du expand/collapse) */
  id: string
  /** Titre du groupe */
  title: string
  /** Icône optionnelle du groupe */
  icon?: typeof LucideIcon
  /** Liens contenus dans ce groupe */
  items: (SidebarLink | SidebarGroup)[]
}

/**
 * Configuration des groupes principaux de la sidebar
 * Ordonnée logiquement : Navigation courante
 */
export const sidebarGroups: SidebarGroup[] = [
  {
    id: 'main-nav',
    title: '',
    items: [
      {
        label: 'Dashboard',
        to: '/reconciliation',
        icon: LayoutDashboard,
        permission: '/reconciliation'
      },
      {
        label: 'Operational',
        to: '/reconciliation/operational',
        icon: ListChecks,
        permission: '/reconciliation/operational'
      },
      {
        label: 'Transversal',
        to: '/reconciliation/transversal',
        icon: GitMerge,
        permission: '/reconciliation/transversal'
      },
      {
        label: 'Lots',
        to: '/reconciliation/lots',
        icon: Boxes,
        permission: '/reconciliation/lots'
      },
      {
        label: 'Runs',
        to: '/reconciliation/runs',
        icon: PlayCircle,
        permission: '/reconciliation/runs'
      },
      {
        label: 'Audit',
        to: '/reconciliation/audit',
        icon: ScrollText,
        permission: '/reconciliation/audit'
      },
      {
        label: 'Archive',
        to: '/reconciliation/archive',
        icon: Archive,
        permission: '/reconciliation/archive'
      },
    ]
  }
]

/**
 * Type pour les items admin : lien simple ou groupe
 */
export type AdminItem = SidebarLink | SidebarGroup

/**
 * Configuration des items admin (affichés en bas de la sidebar)
 * Peut contenir soit des liens simples, soit des groupes avec expand/collapse
 */
export const adminItems: AdminItem[] = [
  // Lien simple
  {
    label: 'Flows',
    to: '/flows',
    icon: Workflow,
    permission: '/flows'
  },
  {
    label: 'Global Settings',
    to: '/admin/settings',
    icon: Settings,
    permission: '/admin/settings'
  },
  

  // Groupe avec expand/collapse
  {
    id: 'user-management',
    title: 'User Management',
    icon: Users,
    items: [
      {
        label: 'Users',
        to: '/users',
        icon: Users,
        permission: '/users'
      },
      {
        label: 'Roles',
        to: '/roles',
        icon: Shield,
        permission: '/roles'
      }
    ]
  }
]

/**
 * Vérifier si un item est un lien simple (pas un groupe)
 */
export const isLink = (item: any): item is SidebarLink => 'to' in item && !('id' in item)

/**
 * Vérifier si un item est un groupe
 */
export const isGroup = (item: any): item is SidebarGroup => 'id' in item && 'items' in item

/**
 * Filtre les items (liens ou groupes) en fonction des permissions
 */
function filterItems(
  items: (SidebarLink | SidebarGroup)[],
  hasPermission: (path: string) => boolean
): (SidebarLink | SidebarGroup)[] {
  return items
    .map(item => {
      // Si c'est un groupe, filtrer récursivement ses items
      if (isGroup(item)) {
        return {
          ...item,
          items: filterItems(item.items, hasPermission)
        }
      }
      // Sinon c'est un lien, on le retourne tel quel
      return item
    })
    // Retirer les groupes vides (après filtrage récursif)
    .filter(item => {
      if (isGroup(item)) {
        return item.items.length > 0
      }
      // Garder les liens qui ont la permission
      return hasPermission((item as SidebarLink).permission)
    })
}

/**
 * Filtre les groupes en fonction des permissions de l'utilisateur
 * Retourne uniquement les groupes ayant au moins 1 lien visible
 * 
 * @param groups Groupes à filtrer
 * @param hasPermission Fonction de vérification des permissions (depuis authStore)
 * @returns Groupes filtrés avec leurs liens accessibles
 */
export function filterSidebarGroups(
  groups: SidebarGroup[],
  hasPermission: (path: string) => boolean
): SidebarGroup[] {
  return groups
    .map(group => ({
      ...group,
      items: filterItems(group.items, hasPermission)
    }))
    .filter(group => group.items.length > 0)
}

/**
 * Filtre les items admin en fonction des permissions de l'utilisateur
 * Les items peuvent être soit des liens simples, soit des groupes
 * 
 * @param items Items admin à filtrer
 * @param hasPermission Fonction de vérification des permissions (depuis authStore)
 * @returns Items admin filtrés avec leurs liens accessibles
 */
export function filterAdminItems(
  items: AdminItem[],
  hasPermission: (path: string) => boolean
): AdminItem[] {
  return items
    .map(item => {
      // Si c'est un groupe, filtrer récursivement ses items
      if (isGroup(item)) {
        return {
          ...item,
          items: filterItems(item.items, hasPermission)
        }
      }
      // Sinon c'est un lien simple
      return item
    })
    // Retirer les groupes vides
    .filter(item => {
      if (isGroup(item)) {
        return (item as SidebarGroup).items.length > 0
      }
      return true
    })
}
