#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <unordered_set>

using namespace std;

// Découpe une ligne CSV simple (sans guillemets contenant le séparateur)
string getColumn(const string& line, int column, char sep=';')
{
    string token;
    stringstream ss(line);

    for(int i = 0; getline(ss, token, sep); i++)
    {
        if(i == column)
            return token;
    }

    return "";
}

int main(int argc, char* argv[])
{
    string fichier1 = argv[1]; // Fichier de référence
    string fichier2 = argv[2]; // Fichier à filtrer
    string fichier3 = argv[3]; // Fichier de sortie


    const int colCleFichier1 = 5; // Colonne contenant la clé dans le fichier de référence
    const int colCleFichier2 = 2; // Colonne contenant la clé dans le fichier à filtrer

    unordered_set<string> cles;

    // Chargement des clés
    ifstream f1(fichier1);
    string ligne;

    while(getline(f1, ligne))
    {
        string cle = getColumn(ligne, colCleFichier1);
        cles.insert(cle);
    }

    f1.close();

    ifstream f2(fichier2);
    ofstream out(fichier3);

    while(getline(f2, ligne))
    {
        string cle = getColumn(ligne, colCleFichier2);

        if(cles.find(cle) != cles.end())
        {
            out << ligne << '\n';
        }
    }

    f2.close();
    out.close();

    cout << "Terminé." << endl;

    return 0;
}