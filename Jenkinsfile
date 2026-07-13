pipeline {
    agent { label 'jenkins-agent' }

    environment {
        // ────────────────────────────────────────────────
        GIT_URL         = 'https://github.com/devsjayanth/Hello-App.git'
        GIT_CRED        = 'github-cred'       
        SONAR_CRED      = 'sonar-cred'        
        DOCKERHUB_USER  = 'devsjayanth'       
        DOCKERHUB_CRED  = 'dockerhub-cred'    
        APP_NAME        = 'hello-app-mongo'         
        APP_PORT        = '9001'              
        APP_INTERNAL    = '8000'              
        DEV_SERVER      = 'dev-server-cred'        
        GITOPS_REPO     = 'https://github.com/devsjayanth/Hello-App-Mongo-GitOps.git'
        GITOPS_BRANCH   = 'main'
        GITOPS_CRED     = 'github-cred'       
        MANIFEST_PATH   = 'k8s/deployment.yml'
        IMAGE_TAG       = "${BUILD_NUMBER}"
        IMAGE_LATEST    = "latest"
        
        IMAGE_NAME      = "${DOCKERHUB_USER}/${APP_NAME}"
        // ────────────────────────────────────────────────
    }

    stages {
        stage('Clean Workspace') {
            steps {
                cleanWs()
            }
        }

        stage('Checkout') {
            steps {
                checkout([
                    $class: 'GitSCM',
                    branches: [[name: '*/main']],
                    userRemoteConfigs: [[
                        url: GIT_URL,
                        credentialsId: GIT_CRED
                    ]]
                ])
            }
        }

        stage('SonarQube Analysis') {
            steps {
                withSonarQubeEnv(installationName: 'sonar-scanner', credentialsId: SONAR_CRED) {
                    sh 'sonar-scanner -Dsonar.projectKey=${APP_NAME} -Dsonar.sources=. -Dsonar.qualitygate.wait=true'
                }
            }
        }

        stage('Build') {
            steps {
                script {
                    docker.build("${IMAGE_NAME}:${IMAGE_TAG}", "--no-cache --pull .")
                    sh "docker tag ${IMAGE_NAME}:${IMAGE_TAG} ${IMAGE_NAME}:${IMAGE_LATEST}"
                }
            }
        }

        stage('Trivy Scan') {
            steps {
                sh "trivy image --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed ${IMAGE_NAME}:${IMAGE_TAG}"
            }
        }

        stage('Push to DockerHub') {
            steps {
                withCredentials([
                    usernamePassword(
                        credentialsId: DOCKERHUB_CRED,
                        usernameVariable: 'DOCKERHUB_USER',
                        passwordVariable: 'DOCKERHUB_PASS'
                    )
                ]) {
                    sh "echo \$DOCKERHUB_PASS | docker login -u \$DOCKERHUB_USER --password-stdin"
                    sh "docker push ${IMAGE_NAME}:${IMAGE_TAG}"
                    sh "docker push ${IMAGE_NAME}:${IMAGE_LATEST}"
                }
            }
        }

        stage('Deploy to Dev Server') {
            steps {
                sh """
                    scp docker-compose-ci.yml ${DEV_SERVER}:~/docker-compose-ci.yml
                    ssh ${DEV_SERVER} '
                        cd ~
                        docker compose -f docker-compose-ci.yml pull
                        docker compose -f docker-compose-ci.yml up -d --remove-orphans
                        docker image prune -f
                    '"""
            }
        }

        stage('Health Check') {
            steps {
                sh """ssh ${DEV_SERVER} '
                    for i in \$(seq 1 12); do
                        curl -sf http://127.0.0.1:${APP_PORT}/ && exit 0
                        sleep 5
                    done
                    exit 1
                '"""
            }
        }

        stage('Approve for Production') {
            steps {
                input message: 'Deploy to K8s via ArgoCD?', ok: 'Yes, deploy to production'
            }
        }

        stage('Push to GitOps Repo') {
            steps {
                withCredentials([usernamePassword(
                    credentialsId: GITOPS_CRED,
                    usernameVariable: 'GIT_USER',
                    passwordVariable: 'GIT_PASS'
                )]) {
                    script {
                        def encodedPass = java.net.URLEncoder.encode(GIT_PASS, "UTF-8")
                        def repoUrl = GITOPS_REPO.replaceFirst(/^https?:\/\//, "https://${GIT_USER}:${encodedPass}@")
                        sh """
                            rm -rf gitops
                            git clone ${repoUrl} gitops
                            cd gitops
                            sed -i 's|image:.*|image: ${IMAGE_NAME}:${IMAGE_TAG}|' ${MANIFEST_PATH}
                            git config user.name 'Jenkins'
                            git config user.email 'jenkins@devops'
                            git add ${MANIFEST_PATH}
                            git commit -m 'deploy: bump ${APP_NAME} to ${IMAGE_TAG}'
                            git push
                        """
                    }
                }
            }
        }
    }
}
